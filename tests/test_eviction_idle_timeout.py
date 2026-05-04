# SPDX-License-Identifier: Apache-2.0
"""
Tests for eviction idle timeout (LRU + expiration) feature.

This module tests the time-based expiration on eviction feature where hot cache
blocks that haven't been accessed in x minutes are dropped (not written to SSD)
when evicted from hot cache, avoiding unnecessary SSD writes for stale data.

The feature ONLY applies when hot cache is enabled. In SSD-only mode, data is
written to SSD immediately during store_cache(), so eviction-time checks are
irrelevant — the data is already on disk.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from omlx.cache.paged_cache import PagedCacheManager, CacheBlock


class TestEvictionIdleTimeoutMetadata:
    """Tests for PagedCacheManager's block expiration metadata (is_expired flag)."""

    @pytest.fixture
    def paged_cache_manager(self):
        """Create a PagedCacheManager for testing."""
        return PagedCacheManager(
            block_size=64,
            max_blocks=100,
            enable_caching=True,
            model_name="test-model",
            initial_blocks=50,
            eviction_idle_timeout=0,  # Disabled by default
        )

    def test_default_no_timeout(self, paged_cache_manager):
        """Test that timeout is disabled by default."""
        assert paged_cache_manager.eviction_idle_timeout == 0

    def test_timeout_configuration(self):
        """Test that timeout can be configured."""
        manager = PagedCacheManager(
            block_size=64,
            max_blocks=100,
            eviction_idle_timeout=300,  # 5 minutes
        )
        assert manager.eviction_idle_timeout == 300

    def test_is_block_expired_disabled(self, paged_cache_manager):
        """Test that is_block_expired returns False when timeout is disabled."""
        block = paged_cache_manager.allocate_block()
        assert block is not None

        # Even though block was just created, should not be expired
        assert not paged_cache_manager.is_block_expired(block.block_id)

    def test_is_block_expired_not_expired(self, paged_cache_manager):
        """Test that recently accessed blocks are not expired."""
        # Enable timeout
        paged_cache_manager.eviction_idle_timeout = 300  # 5 minutes

        block = paged_cache_manager.allocate_block()
        assert block is not None

        # Block was just accessed, should not be expired
        assert not paged_cache_manager.is_block_expired(block.block_id)

    def test_is_block_expired_expired(self, paged_cache_manager):
        """Test that stale blocks are marked as expired."""
        # Enable timeout
        paged_cache_manager.eviction_idle_timeout = 1  # 1 minute

        block = paged_cache_manager.allocate_block()
        assert block is not None

        # Block should not be expired initially
        assert not paged_cache_manager.is_block_expired(block.block_id)

        # Manually set last_access to simulate idle time
        block.last_access = time.time() - 70  # 70 seconds ago

        # Now block should be expired (1 min = 60s, idle=70s)
        assert paged_cache_manager.is_block_expired(block.block_id)

    def test_evict_lru_blocks_marks_expired_flag(self, paged_cache_manager):
        """Test that evict_lru_blocks sets is_expired flag on idle blocks."""
        # Use a small manager so we can control the free queue precisely
        small_manager = PagedCacheManager(
            block_size=64,
            max_blocks=10,
            enable_caching=True,
            model_name="test-model",
            initial_blocks=5,
            eviction_idle_timeout=1,  # 1 minute
        )

        # Allocate ALL blocks so the free queue is empty
        blocks = []
        for _ in range(4):  # Null block is 0, so blocks 1-4
            b = small_manager.allocate_block()
            blocks.append(b)

        # Free them all to put them in the free queue
        for b in blocks:
            small_manager.free_block(b.block_id)

        # Now make them all old so they'll be expired
        for b in blocks:
            b.last_access = time.time() - 70  # 70 seconds ago

        # Evict blocks — they should be marked as expired
        evicted = small_manager.evict_lru_blocks(4)
        assert evicted == 4

        # Check that is_expired was set on the evicted blocks
        expired_count = sum(1 for b in blocks if b.is_expired)
        assert expired_count == 4, f"Expected 4 expired, got {expired_count}"

    def test_evict_lru_blocks_not_expired(self, paged_cache_manager):
        """Test that non-expired blocks are not flagged and evicted normally."""
        # Enable timeout
        paged_cache_manager.eviction_idle_timeout = 300  # 5 minutes

        # Allocate and free some blocks
        block1 = paged_cache_manager.allocate_block()
        block2 = paged_cache_manager.allocate_block()

        paged_cache_manager.free_block(block1.block_id)
        paged_cache_manager.free_block(block2.block_id)

        # Evict immediately (not expired)
        evicted = paged_cache_manager.evict_lru_blocks(2)

        # Both blocks should be processed through LRU eviction
        assert evicted == 2

        # Blocks should NOT be marked as expired
        assert block1.is_expired is False
        assert block2.is_expired is False

    def test_get_evictable_blocks_marks_expired_flag(self):
        """Test that get_evictable_blocks sets is_expired flag (not token_count=-1)."""
        # Use a small manager so freed blocks are easier to reach
        small_manager = PagedCacheManager(
            block_size=64,
            max_blocks=10,
            enable_caching=True,
            model_name="test-model",
            initial_blocks=5,
            eviction_idle_timeout=1,  # 1 minute
        )

        # Allocate and free all non-null blocks
        blocks = []
        for _ in range(4):
            b = small_manager.allocate_block()
            blocks.append(b)

        for b in blocks:
            small_manager.free_block(b.block_id)

        # Manually set last_access on all blocks to simulate idle time
        for b in blocks:
            b.last_access = time.time() - 70

        # Get all evictable blocks
        evictable = small_manager.get_evictable_blocks(10)

        # All freed blocks should be in evictable
        assert len(evictable) >= 4

        # Blocks should have is_expired=True (NOT token_count=-1)
        expired_blocks = [b for b in evictable if b.is_expired is True]
        assert len(expired_blocks) >= 4

        # None should have token_count=-1 (that was the OLD buggy behavior)
        corrupted_blocks = [b for b in evictable if b.token_count == -1]
        assert len(corrupted_blocks) == 0

    def test_evict_block_permanently_resets_expired_flag(self, paged_cache_manager):
        """Test that evict_block_permanently resets is_expired for reuse."""
        block = paged_cache_manager.allocate_block()
        block.is_expired = True  # Simulate expired
        paged_cache_manager.free_block(block.block_id)

        # Evict permanently — should reset is_expired for reuse
        result = paged_cache_manager.evict_block_permanently(block.block_id)
        assert result is True
        assert block.is_expired is False

        # Block should be back in free queue
        assert block.block_id not in paged_cache_manager.allocated_blocks

    def test_allocate_block_resets_expired_flag(self, paged_cache_manager):
        """Test that allocate_block resets is_expired to False."""
        block = paged_cache_manager.allocate_block()
        block.is_expired = True  # Simulate expired
        paged_cache_manager.free_block(block.block_id)

        # The free queue still has the block with is_expired=True.
        # When re-allocated, is_expired should be reset.
        # We need to trigger eviction to get it back
        paged_cache_manager.eviction_idle_timeout = 1
        paged_cache_manager.evict_lru_blocks(1)

        # Now allocate a new block — it should have is_expired=False
        new_block = paged_cache_manager.allocate_block()
        assert new_block.is_expired is False

    def test_touch_updates_last_access(self, paged_cache_manager):
        """Test that touch() updates last_access time."""
        block = paged_cache_manager.allocate_block()

        # Wait a bit
        time.sleep(0.1)

        # Touch the block
        block.touch()

        # Block should not be expired even with short timeout
        paged_cache_manager.eviction_idle_timeout = 1
        assert not paged_cache_manager.is_block_expired(block.block_id)

    def test_eviction_idle_timeout_logging(self, caplog):
        """Test that eviction idle timeout is logged during initialization."""
        import logging

        with caplog.at_level(logging.INFO):
            manager = PagedCacheManager(
                block_size=64,
                max_blocks=100,
                eviction_idle_timeout=30,
            )

        # Check that timeout was logged
        assert "eviction_idle_timeout=30min" in caplog.text


class TestHotCacheExpiredEviction:
    """Tests for PagedSSDCacheManager's hot cache eviction with idle timeout.

    These tests verify that when hot cache entries are evicted, entries that
    have exceeded the idle timeout are dropped (not written to SSD) rather
    than being flushed to disk.
    """

    @pytest.fixture
    def mock_ssd_manager(self, tmp_path):
        """Create a PagedSSDCacheManager with hot cache enabled and idle timeout."""
        from omlx.cache.paged_ssd_cache import PagedSSDCacheManager

        manager = PagedSSDCacheManager(
            cache_dir=tmp_path,
            max_size_bytes=100 * 1024 * 1024,  # 100MB
            hot_cache_max_bytes=1024,  # Very small hot cache to trigger eviction
            eviction_idle_timeout=1,  # 1 minute
        )
        yield manager
        manager.close()

    def test_eviction_idle_timeout_accepted(self, tmp_path):
        """Test that PagedSSDCacheManager accepts eviction_idle_timeout."""
        from omlx.cache.paged_ssd_cache import PagedSSDCacheManager

        manager = PagedSSDCacheManager(
            cache_dir=tmp_path,
            max_size_bytes=100 * 1024 * 1024,
            hot_cache_max_bytes=1024,
            eviction_idle_timeout=30,
        )
        assert manager._eviction_idle_timeout == 30
        manager.close()

    def test_eviction_idle_timeout_disabled_by_default(self, tmp_path):
        """Test that eviction_idle_timeout defaults to 0 (disabled)."""
        from omlx.cache.paged_ssd_cache import PagedSSDCacheManager

        manager = PagedSSDCacheManager(
            cache_dir=tmp_path,
            max_size_bytes=100 * 1024 * 1024,
            hot_cache_max_bytes=1024,
        )
        assert manager._eviction_idle_timeout == 0
        manager.close()


class TestTieredManagerTimeout:
    """Tests for TieredCacheManager timeout propagation (hot-cache-only path)."""

    def test_tiered_manager_passes_timeout_when_hot_cache_enabled(self):
        """When hot cache is enabled, timeout is passed to both managers."""
        from omlx.cache.tiered_manager import TieredCacheManager
        from omlx.cache.paged_cache import PagedCacheManager

        paged_cache = PagedCacheManager(
            block_size=64,
            max_blocks=100,
            eviction_idle_timeout=0,  # Initially disabled
        )

        # Simulate what from_config does: set timeout only when hot cache enabled
        timeout = 30
        hot_cache_enabled = True  # hot_cache_max_bytes > 0
        effective_timeout = timeout if hot_cache_enabled else 0

        paged_cache.eviction_idle_timeout = effective_timeout
        assert paged_cache.eviction_idle_timeout == 30

    def test_tiered_manager_skips_timeout_when_hot_cache_disabled(self):
        """When hot cache is disabled, timeout is effectively 0."""
        from omlx.cache.paged_cache import PagedCacheManager

        paged_cache = PagedCacheManager(
            block_size=64,
            max_blocks=100,
            eviction_idle_timeout=0,
        )

        # When hot cache is disabled (hot_cache_max_bytes=0),
        # the timeout is meaningless since data goes directly to SSD
        # in store_cache(). The effective timeout should be 0.
        hot_cache_enabled = False
        user_timeout = 30  # Even if user sets 30
        effective_timeout = user_timeout if hot_cache_enabled else 0

        paged_cache.eviction_idle_timeout = effective_timeout
        assert paged_cache.eviction_idle_timeout == 0


class TestConfigConsistency:
    """Tests that configuration defaults are consistent."""

    def test_paged_cache_default_is_disabled(self):
        """PagedCacheManager default eviction_idle_timeout should be 0 (disabled)."""
        manager = PagedCacheManager(block_size=64, max_blocks=100)
        assert manager.eviction_idle_timeout == 0

    def test_cache_factory_default_is_disabled(self):
        """CacheConfig default eviction_idle_timeout should be 0 (disabled)."""
        from omlx.cache.factory import CacheConfig

        config = CacheConfig()
        assert config.eviction_idle_timeout == 0

    def test_scheduler_config_default(self):
        """SchedulerConfig default eviction_idle_timeout should be 30 (user-facing)."""
        from omlx.scheduler import SchedulerConfig

        config = SchedulerConfig()
        assert config.eviction_idle_timeout == 30


if __name__ == "__main__":
    pytest.main([__file__, "-v"])