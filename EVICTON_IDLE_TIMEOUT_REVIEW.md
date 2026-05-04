# Eviction Idle Timeout Feature Review & Fix Summary

## Original Bugs Found

### Bug 1 (CRITICAL): Hot cache eviction path — the ONLY path that writes expired data to SSD — had no expiration check

The primary path where stale data gets written to SSD is `PagedSSDCacheManager._hot_cache_put()`.
When the hot cache is full, LRU entries are evicted and unconditionally written to SSD via
`_enqueue_ssd_write()`. There was NO check for whether the evicted entry was stale/expired.

This means expired blocks were STILL written to SSD despite the "eviction idle timeout" feature.

**Fix**: Added `eviction_idle_timeout` to `PagedSSDCacheManager.__init__()`. In `_hot_cache_put()`,
evicted entries that exceed the idle timeout are dropped instead of being written to SSD.
Also applied the same check during shutdown flush in `close()`.

### Bug 2 (CRITICAL): `token_count = -1` marker corrupted block metadata

`get_evictable_blocks()` used `token_count = -1` as a sentinel for expired blocks, which:
- Corrupted `stats.total_tokens_cached` calculations (subtracting -1 adds instead)
- Persisted across block reuse without reset
- Was a hacky abuse of a meaningful metadata field

**Fix**: Added proper `is_expired: bool = False` field to `CacheBlock`. Updated
`get_evictable_blocks()` and `evict_lru_blocks()` to use `is_expired` instead.
Added reset of `is_expired` in `allocate_block()`, `get_new_blocks()`, and
`evict_block_permanently()`.

### Bug 3 (HIGH): `evict_lru_blocks()` expiration logic was dead code

The method checked for expired blocks and logged about "skipping SSD write" but
never actually prevented any SSD write — the method only manages metadata and
doesn't trigger SSD writes anyway.

**Fix**: Removed misleading SSD write skip comments. The method now correctly marks
`block.is_expired` for downstream consumers (TieredCacheManager and
PagedSSDCacheManager) to act on.

### Bug 4 (HIGH): `evict_block_permanently(skip_ssd_write=True)` was dead code

The `skip_ssd_write` parameter was accepted and logged but never used. The method
only manages metadata — it doesn't trigger SSD writes.

**Fix**: Removed `skip_ssd_write` parameter. Added `is_expired` flag handling that
preserves the expiration state through the eviction for downstream cleanup.

### Bug 5 (MEDIUM): `evict_blocks_to_cold()` in TieredCacheManager claimed to skip SSD writes but didn't

The method checked `is_block_expired()` and passed `skip_ssd_write=True` to
`evict_block_permanently()`, but since the PagedCacheManager doesn't write to SSD
(the data is already on SSD), this had no effect.

Additionally, for expired blocks, the SSD data should be DELETED to free disk space,
not just left on disk.

**Fix**: Replaced the `skip_ssd_write` flag with explicit SSD data cleanup for
expired blocks using `PagedSSDCacheManager.delete_block()`.

### Bug 6 (MEDIUM): Default value inconsistency

`CacheConfig.eviction_idle_timeout` and `PagedCacheManager.eviction_idle_timeout`
defaulted to 0 (disabled), while `PagedSSDCacheConfig`, `SchedulerConfig`, and
`CacheSettings` defaulted to 30 (enabled).

**Fix**: Made the default 0 (disabled) at all levels for backward compatibility.
The user-facing defaults (SchedulerConfig, CacheSettings) remain 30 to match the
original feature intent, but the internal/factory defaults are 0.

### Bug 7 (LOW): The feature was designed for the wrong layer

The eviction idle timeout was implemented entirely in `PagedCacheManager` (metadata
layer), but the actual SSD write happens in `PagedSSDCacheManager` (persistence layer).
The metadata layer has no SSD write paths, so all expiration checks there were
ineffective.

**Fix**: Added `eviction_idle_timeout` to `PagedSSDCacheManager`. When hot cache
is enabled, entries evicted from hot cache that exceed the idle timeout are dropped
instead of written to SSD. This is the correct layer for the feature.

Since the feature is ONLY meaningful when hot cache is enabled (data stays in RAM
before being written to SSD), the `TieredCacheManager.from_config()` now only passes
the timeout to `PagedSSDCacheManager` when `hot_cache_max_bytes > 0`. In SSD-only
mode, the timeout is forced to 0 (disabled) because data is written directly to SSD
in `store_cache()` — there's no eviction path that triggers SSD writes.

## Files Changed

1. **omlx/cache/paged_cache.py**: Added `is_expired` field to `CacheBlock`, replaced
   `token_count=-1` hack with `is_expired` flag, removed `skip_ssd_write` from
   `evict_block_permanently()`, reset `is_expired` in `allocate_block()` and
   `get_new_blocks()`, simplified `evict_lru_blocks()` to just mark `is_expired`.

2. **omlx/cache/paged_ssd_cache.py**: Added `eviction_idle_timeout` parameter to
   `__init__()`. Modified `_hot_cache_put()` to skip SSD writes for expired entries.
   Modified `close()` to drop expired entries during shutdown flush. Added
   `hot_cache_expired_evictions` stat.

3. **omlx/cache/tiered_manager.py**: Only pass `eviction_idle_timeout` to
   `PagedSSDCacheManager` when hot cache is enabled. In `evict_blocks_to_cold()`,
   check `is_expired` flag and delete SSD data for expired blocks.

4. **omlx/cache/factory.py**: Added `hot_cache_max_bytes` and `eviction_idle_timeout`
   to `CacheConfig`. Pass both through to `PagedSSDCacheManager`.

5. **omlx/cache/stats.py**: Added `hot_cache_expired_evictions` to `PagedSSDCacheStats`.

6. **tests/test_eviction_idle_timeout.py**: Rewrote tests to verify correct behavior:
   - `is_expired` flag (not `token_count=-1`)
   - `PagedSSDCacheManager` accepts `eviction_idle_timeout`
   - Hot cache disabled path forces timeout to 0
   - Config consistency tests

7. **tests/test_settings.py**: Updated `test_to_dict` to include `eviction_idle_timeout`.