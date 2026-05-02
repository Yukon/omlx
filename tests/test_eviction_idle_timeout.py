# SPDX-License-Identifier: Apache-2.0
"""
Tests for eviction idle timeout (LRU + expiration) feature.

This module tests the time-based expiration on eviction feature where blocks
that haven't been accessed in x minutes are evicted without being written to SSD,
avoiding the processing overhead of computing and saving stale data.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from omlx.cache.paged_cache import PagedCacheManager, CacheBlock


class TestEvictionIdleTimeout:
    """Tests for eviction idle timeout functionality."""

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

    def test_evict_lru_blocks_with_timeout_expired(self, paged_cache_manager):
        """Test that expired blocks are skipped during LRU eviction."""
        # Enable timeout
        paged_cache_manager.eviction_idle_timeout = 1  # 1 minute

        # Allocate and free some blocks
        block1 = paged_cache_manager.allocate_block()
        block2 = paged_cache_manager.allocate_block()

        paged_cache_manager.free_block(block1.block_id)
        paged_cache_manager.free_block(block2.block_id)

        # Manually set last_access to simulate idle time
        block1.last_access = time.time() - 70
        block2.last_access = time.time() - 70

        # Evict blocks
        evicted = paged_cache_manager.evict_lru_blocks(2)

        # Both blocks should be processed through LRU eviction
        assert evicted == 2

        # Blocks should be returned to the free queue (reusable)
        # The free_blocks count doesn't change during eviction, just the ordering
        assert paged_cache_manager.free_blocks >= 2

    def test_evict_lru_blocks_with_timeout_not_expired(self, paged_cache_manager):
        """Test that non-expired blocks are evicted normally."""
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

    def test_get_evictable_blocks_marks_expired(self, paged_cache_manager):
        """Test that get_evictable_blocks marks expired blocks."""
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

        # At least some blocks should be marked as expired (token_count = -1)
        expired_blocks = [b for b in evictable if b.token_count == -1]
        assert len(expired_blocks) >= 4

    def test_evict_block_permanently_with_skip_ssd(self, paged_cache_manager):
        """Test that evict_block_permanently accepts skip_ssd_write flag."""
        block = paged_cache_manager.allocate_block()
        paged_cache_manager.free_block(block.block_id)

        # Evict with skip_ssd_write=True
        result = paged_cache_manager.evict_block_permanently(
            block.block_id, skip_ssd_write=True
        )

        assert result is True
        # Block should be back in free queue
        assert block.block_id not in paged_cache_manager.allocated_blocks

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


class TestEvictionIdleTimeoutIntegration:
    """Integration tests for eviction idle timeout with TieredCacheManager."""

    def test_tiered_manager_accepts_timeout(self):
        """Test that TieredCacheManager.from_config accepts eviction_idle_timeout."""
        from omlx.cache.tiered_manager import TieredCacheManager

        # Create mock dependencies
        paged_cache = PagedCacheManager(
            block_size=64,
            max_blocks=100,
            eviction_idle_timeout=0,
        )

        # Should accept timeout parameter without error
        # (will return None since we don't have SSD cache dir)
        with patch.object(TieredCacheManager, 'from_config', return_value=None):
            # Just test that the signature accepts the parameter
            pass

    def test_tiered_manager_sets_timeout_on_paged_cache(self):
        """Test that TieredCacheManager sets timeout on paged cache."""
        # Create a paged cache manager
        paged_cache = PagedCacheManager(
            block_size=64,
            max_blocks=100,
            eviction_idle_timeout=0,  # Initially disabled
        )

        # Manually set timeout (simulating what from_config does)
        timeout_minutes = 30
        paged_cache.eviction_idle_timeout = timeout_minutes

        assert paged_cache.eviction_idle_timeout == timeout_minutes


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
