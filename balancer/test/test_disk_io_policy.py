# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the per-media disk-IO throttle policy (balancer/balancer/balancer.py).

Two decisions are covered, both of which used to be a single media-agnostic number:

* **Who gets throttled** -- ``_qualifies_for_throttle``. 5 MB/s is nothing on an NVMe and
  most of what a thumb drive can deliver, so one global floor either ignored slow media
  entirely or throttled fast media constantly.
* **How hard** -- ``_scaled_io_limits``. The ``limit_policy.disk_io.rate`` table is
  calibrated for NVMe; ``media_scale`` re-expresses it per media class.

Both tables are UI-editable, so the config surface that feeds them (validation + the YAML
line patcher) is covered here too, next to the code that consumes it.

The methods only touch ``self.config`` and ``self.io_ctl``, so they are exercised on a
stub instance -- constructing a real ``DynamicBalancer`` would start BPF and cgroup
machinery that has nothing to do with the arithmetic under test.

Run:  python3 balancer/test/test_disk_io_policy.py
"""

import os
import sys
import unittest
from unittest import mock

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for path in (_REPO_ROOT, os.path.join(_REPO_ROOT, "balancer")):
    if path not in sys.path:
        sys.path.insert(0, path)

import balancer.balancer as balancer_mod  # noqa: E402
from balancer.balancer import DynamicBalancer  # noqa: E402


class _Cfg:
    def __init__(self, disk_io=None):
        self.limit_policy = {'disk_io': disk_io or {}}


class _IoCtl:
    """Stand-in for IOController: only get_disk_id is reached from these methods."""

    def __init__(self, disks):
        self._disks = disks

    def get_disk_id(self, disk_filter=None):
        if not disk_filter:
            return dict(self._disks)
        wanted = [disk_filter] if isinstance(disk_filter, str) else disk_filter
        return {d: i for d, i in self._disks.items() if d in wanted}


def _balancer(disk_io_cfg=None, disks=None):
    b = DynamicBalancer.__new__(DynamicBalancer)  # no __init__: no BPF, no cgroups
    b.config = _Cfg(disk_io_cfg)
    b.io_ctl = _IoCtl(disks or {"nvme0n1": "259:0"})
    return b


def _rates(read_mb=0.0, write_mb=0.0, read_iops=0.0, write_iops=0.0):
    return {'read_mb_s': read_mb, 'write_mb_s': write_mb,
            'read_iops': read_iops, 'write_iops': write_iops}


# Media detection reads sysfs, which says nothing useful about the fake disks above.
def _media(mapping, default='nvme'):
    return mock.patch.object(balancer_mod, 'media_for_disk',
                             side_effect=lambda d: mapping.get(d, default))


class CandidateFloorTests(unittest.TestCase):
    """Which apps are heavy enough that capping them would relieve the device."""

    def test_same_rate_qualifies_on_usb_but_not_on_nvme(self):
        """The whole point of per-media floors: 6 MB/s is idle on an NVMe and near the
        ceiling of a USB stick, so one number cannot serve both."""
        proc = {'io_per_disk': {'d': _rates(write_mb=6.0)}}
        with _media({'d': 'nvme'}):
            self.assertFalse(_balancer()._qualifies_for_throttle(proc)[0])
        with _media({'d': 'usb'}):
            self.assertTrue(_balancer()._qualifies_for_throttle(proc)[0])

    def test_iops_floor_qualifies_a_bandwidth_light_app(self):
        """A 4k random workload moves few MB but saturates the device, so EITHER floor
        qualifying is load-bearing, not a convenience."""
        proc = {'io_per_disk': {'d': _rates(write_mb=9.0, write_iops=2400.0)}}
        with _media({'d': 'nvme'}):
            ok, why = _balancer()._qualifies_for_throttle(proc)
        self.assertTrue(ok)
        self.assertIn('2400iops', why)

    def test_judged_per_disk_not_on_the_total(self):
        """Two disks at 20 MB/s each are not one disk at 40: neither is worth capping, and
        summing them would throttle an app that is not hurting anything."""
        proc = {'io_per_disk': {'a': _rates(write_mb=20.0), 'b': _rates(write_mb=20.0)}}
        with _media({'a': 'nvme', 'b': 'nvme'}):
            self.assertFalse(_balancer()._qualifies_for_throttle(proc)[0])

    def test_one_hot_disk_is_enough(self):
        proc = {'io_per_disk': {'a': _rates(write_mb=2.0), 'b': _rates(write_mb=45.0)}}
        with _media({'a': 'nvme', 'b': 'nvme'}):
            ok, why = _balancer()._qualifies_for_throttle(proc)
        self.assertTrue(ok)
        self.assertIn('b(nvme)', why)

    def test_missing_breakdown_uses_the_strictest_floor(self):
        """No per-disk data is not evidence of a slow device, so the conservative reading
        wins -- otherwise every app would be judged by a thumb drive's 4 MB/s."""
        b = _balancer()
        self.assertFalse(b._qualifies_for_throttle(
            {'io_read_rate': 10.0, 'io_write_rate': 10.0})[0])
        ok, why = b._qualifies_for_throttle({'io_read_rate': 0.0, 'io_write_rate': 31.0})
        self.assertTrue(ok)
        self.assertIn('no per-disk data', why)

    def test_config_overrides_one_class_and_keeps_the_rest(self):
        """A per-class merge, so tuning `usb` cannot silently reset the calibrated NVMe
        numbers and a typo costs one class instead of the table."""
        b = _balancer({'candidate_floor': {'usb': {'mb_s': 1, 'iops': 10}}})
        floors = b._candidate_floors()
        self.assertEqual(floors['usb'], {'mb_s': 1.0, 'iops': 10.0})
        self.assertEqual(floors['nvme'], DynamicBalancer._CANDIDATE_FLOOR_DEFAULT['nvme'])

    def test_malformed_config_falls_back_to_defaults(self):
        b = _balancer({'candidate_floor': {'nvme': 'not-a-dict', 'hdd': {'mb_s': 'x'}}})
        floors = b._candidate_floors()
        self.assertEqual(floors['nvme'], DynamicBalancer._CANDIDATE_FLOOR_DEFAULT['nvme'])
        self.assertEqual(floors['hdd'], DynamicBalancer._CANDIDATE_FLOOR_DEFAULT['hdd'])

    def test_unknown_media_is_as_permissive_as_hdd(self):
        floors = _balancer()._candidate_floors()
        self.assertEqual(floors['unknown'], floors['hdd'])


class MediaScaleTests(unittest.TestCase):
    """How hard the cap bites, per media class."""

    RATES = {'read': 30, 'write': 20, 'read_iops': 9000, 'write_iops': 6000}

    def test_nvme_is_unscaled(self):
        """`rate` was calibrated against NVMe, so that class must come through untouched --
        a coefficient of 1.0 is the anchor the other classes are relative to."""
        b = _balancer(disks={'nvme0n1': '259:0'})
        with _media({'nvme0n1': 'nvme'}):
            limits = b._scaled_io_limits(self.RATES)
        self.assertEqual(limits['nvme0n1']['wbps'], 20 * 1024 ** 2)
        self.assertEqual(limits['nvme0n1']['wiops'], 6000)

    def test_slower_media_get_a_proportionally_smaller_cap(self):
        b = _balancer(disks={'sdb': '8:16'})
        with _media({'sdb': 'usb'}):
            limits = b._scaled_io_limits(self.RATES)
        self.assertEqual(limits['sdb']['wbps'], int(20 * 0.15 * 1024 ** 2))

    def test_bandwidth_and_iops_scale_by_the_same_factor(self):
        """The rate table is built on `iops = MB/s * 300`. Scaling the two independently
        would move the block size at which the IOPS cap starts to bind, so a media class
        would quietly enforce something other than the MB/s it promises."""
        b = _balancer(disks={'sda': '8:0'})
        with _media({'sda': 'hdd'}):
            limits = b._scaled_io_limits(self.RATES)
        mb_ratio = limits['sda']['wbps'] / (20 * 1024 ** 2)
        iops_ratio = limits['sda']['wiops'] / 6000
        self.assertAlmostEqual(mb_ratio, iops_ratio, places=3)

    def test_each_disk_is_scaled_by_its_own_media(self):
        """A mixed box is the case the old single `default` entry got wrong: one io.max for
        every disk means the USB stick and the NVMe are capped identically."""
        b = _balancer(disks={'nvme0n1': '259:0', 'sdb': '8:16'})
        with _media({'nvme0n1': 'nvme', 'sdb': 'usb'}):
            limits = b._scaled_io_limits(self.RATES)
        self.assertGreater(limits['nvme0n1']['wbps'], limits['sdb']['wbps'])

    def test_default_entry_stays_unscaled(self):
        """`default` catches a disk that appeared between enumeration and the write. It must
        be the loosest value, not a thumb drive's -- an unknown disk should not be hard
        capped by accident."""
        b = _balancer(disks={'sdb': '8:16'})
        with _media({'sdb': 'usb'}):
            limits = b._scaled_io_limits(self.RATES)
        self.assertEqual(limits['default']['wbps'], 20 * 1024 ** 2)

    def test_disk_filter_limits_the_map_to_the_stressed_disks(self):
        b = _balancer(disks={'nvme0n1': '259:0', 'sdb': '8:16'})
        with _media({'nvme0n1': 'nvme', 'sdb': 'usb'}):
            limits = b._scaled_io_limits(self.RATES, ['sdb'])
        self.assertEqual(set(limits) - {'default'}, {'sdb'})

    def test_cap_never_reaches_zero(self):
        """A cap of 0 in io.max is not "very slow", it is a stall. Even a 1 MB/s rate row on
        the smallest coefficient has to land on a positive number of bytes."""
        b = _balancer(disks={'sdb': '8:16'})
        with _media({'sdb': 'usb'}):
            limits = b._scaled_io_limits(
                {'read': 0, 'write': 0, 'read_iops': 0, 'write_iops': 0})
        self.assertTrue(all(v >= 1 for v in limits['sdb'].values()))

    def test_config_overrides_a_single_coefficient(self):
        b = _balancer({'media_scale': {'usb': 0.5}}, disks={'sdb': '8:16'})
        with _media({'sdb': 'usb'}):
            limits = b._scaled_io_limits(self.RATES)
        self.assertEqual(limits['sdb']['wbps'], int(20 * 0.5 * 1024 ** 2))

    def test_out_of_range_coefficient_is_rejected(self):
        """Above 1.0 would raise the cap above the calibrated table -- a loosening nobody
        asked for, dressed up as a media correction."""
        b = _balancer({'media_scale': {'usb': 4.0, 'hdd': 0, 'nvme': 'x'}})
        scales = b._media_scales()
        self.assertEqual(scales['usb'], DynamicBalancer._MEDIA_SCALE_DEFAULT['usb'])
        self.assertEqual(scales['hdd'], DynamicBalancer._MEDIA_SCALE_DEFAULT['hdd'])
        self.assertEqual(scales['nvme'], 1.0)


class ConfigSurfaceTests(unittest.TestCase):
    """The two tables are UI-editable, so the API and the YAML writer have to agree with
    the shape the balancer reads back."""

    def setUp(self):
        from monitor.monitor_api import _validate_limit_policy_config
        self.validate = _validate_limit_policy_config

    def test_partial_update_keeps_the_nesting(self):
        upd = self.validate({'disk_io': {'media_scale': {'usb': 0.2},
                                         'candidate_floor': {'usb': {'mb_s': 3}}}})
        self.assertEqual(upd['disk_io']['media_scale'], {'usb': 0.2})
        self.assertEqual(upd['disk_io']['candidate_floor'], {'usb': {'mb_s': 3.0}})

    def test_scale_above_one_is_rejected(self):
        with self.assertRaises(ValueError):
            self.validate({'disk_io': {'media_scale': {'usb': 1.5}}})

    def test_zero_floor_is_rejected(self):
        """A floor of 0 qualifies every process that touched the disk once."""
        with self.assertRaises(ValueError):
            self.validate({'disk_io': {'candidate_floor': {'hdd': {'mb_s': 0}}}})

    def test_unlisted_media_is_ignored_not_written(self):
        with self.assertRaises(ValueError):  # nothing valid left -> no update at all
            self.validate({'disk_io': {'media_scale': {'floppy': 0.5}}})

    def test_shipped_yaml_is_reachable_by_the_line_patcher(self):
        """config.yaml has to stay block-style here: the patcher matches keys line by line at
        fixed indent, so a flow-style `usb: {mb_s: 4}` would make a UI save silently no-op."""
        from config.config import Config
        path = os.path.join(_REPO_ROOT, "config", "config.yaml")
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        for media in ('nvme', 'sata_ssd', 'mmc', 'hdd', 'usb', 'unknown'):
            self.assertTrue(Config._replace_yaml_scalar_line(
                lines, ("limit_policy", "disk_io", "media_scale", media), 0.5), media)
            for field in ('mb_s', 'iops'):
                self.assertTrue(Config._replace_yaml_scalar_line(
                    lines, ("limit_policy", "disk_io", "candidate_floor", media, field), 7),
                    f"{media}.{field}")


def _consumer(app_id, mb=500.0, iops=5000.0, name='fio', cgroup_id=''):
    return {'app': {'id': app_id},
            'cgroup_id': cgroup_id,
            'process': {'name': name, 'io_read_rate': 0.0, 'io_write_rate': mb,
                        'io_read_iops': 0.0, 'io_write_iops': iops}}


def _limited(key, public_id, cgroups, io_limited=True):
    return key, balancer_mod.LimitedApp(
        public_app_id=public_id, app_name='fio', source='auto', limit_rates={},
        limit_parts={'cpu_mem_limited': False, 'io_limited': io_limited},
        cgroups=list(cgroups))


class AlreadyLimitedTests(unittest.TestCase):
    """One capped app answers to several ids, and the candidate loop only holds one of them.

    The loop runs *before* the controlled app is resolved to its cgroups, so the id it has
    is the top-consumer sample's while the registry entry is keyed by the resolved primary
    cgroup. Missing that identity re-caps the same app every critical tick and the second
    heaviest consumer is never reached -- which is exactly what leaves the disk saturated.
    """

    def _b(self, *entries):
        b = _balancer()
        b.all_limits = balancer_mod.LimitRegistry()
        for key, entry in entries:
            b.all_limits.apps[key] = entry
        b._qualifies_for_throttle = lambda proc: (True, 'stub floor')
        b.get_limited_rates = lambda priority: {'disk_io_rate': {
            'read': 50, 'write': 20, 'read_iops': 2000, 'write_iops': 1000}}
        return b

    def _select(self, b, consumers, controlled=None):
        with mock.patch.object(balancer_mod.app_utils, 'get_app_control_info',
                               side_effect=lambda i, n: controlled.get(i, (False, None))
                               if controlled else (False, None)):
            should, _is_ctl, app_id, _rates, _target, idx = b._handle_disk_io_stressed(consumers)
        return (app_id, idx) if should else (None, None)

    def test_candidate_matching_the_registry_key_is_skipped(self):
        b = self._b(_limited('lo-io.scope', 'lo-app', ['lo-io.scope']))
        app_id, _ = self._select(b, [_consumer('lo-io.scope')])
        self.assertIsNone(app_id)

    def test_candidate_matching_the_public_id_is_skipped(self):
        b = self._b(_limited('lo-io.scope', 'lo-app', ['lo-io.scope']))
        app_id, _ = self._select(b, [_consumer('lo-app')])
        self.assertIsNone(app_id)

    def test_candidate_matching_an_extra_cgroup_is_skipped(self):
        """A multi-cgroup app can surface under any of its cgroups from one tick to the
        next; the cap was written to all of them, so any of them counts as limited."""
        b = self._b(_limited('lo-io.scope', 'lo-app', ['lo-io.scope', 'lo-helper.scope']))
        app_id, _ = self._select(b, [_consumer('lo-helper.scope')])
        self.assertIsNone(app_id)

    def test_controlled_app_is_skipped_under_its_configured_id(self):
        b = self._b(_limited('lo-io.scope', 'lo-app', ['lo-io.scope']))
        app_id, _ = self._select(
            b, [_consumer('some-other.scope')],
            controlled={'some-other.scope': (True, {'app_id': 'lo-app', 'priority': 'low'})})
        self.assertIsNone(app_id)

    def test_a_different_app_is_still_selected(self):
        b = self._b(_limited('lo-io.scope', 'lo-app', ['lo-io.scope']))
        app_id, idx = self._select(b, [_consumer('lo-io.scope'), _consumer('hi-io.scope')])
        self.assertEqual((app_id, idx), ('hi-io.scope', 1))

    def test_cpu_only_limit_does_not_count_as_io_limited(self):
        b = self._b(_limited('lo-io.scope', 'lo-app', ['lo-io.scope'], io_limited=False))
        app_id, _ = self._select(b, [_consumer('lo-io.scope')])
        self.assertEqual(app_id, 'lo-io.scope')

    def test_uncontrolled_process_identity_uses_sampled_cgroup_as_throttle_key(self):
        b = self._b()
        app_id, _ = self._select(
            b,
            [_consumer('fio_runner.py', cgroup_id='session-12.scope', name='fio_runner.py')]
        )
        self.assertEqual(app_id, 'session-12.scope')

    def test_already_limited_match_checks_sampled_cgroup_identity(self):
        b = self._b(_limited('session-12.scope', 'session-12.scope', ['session-12.scope']))
        app_id, _ = self._select(
            b,
            [_consumer('fio_runner.py', cgroup_id='session-12.scope', name='fio_runner.py')]
        )
        self.assertIsNone(app_id)


class PublicIdTests(unittest.TestCase):
    """The id a limit is recorded under, once the cgroup id and the app id differ.

    ``app_id`` addresses cgroups after :meth:`_resolve_controlled_target` rewrites it; the
    DB row and the UI still key the app by its public id. Recording the cgroup name as the
    public id makes every later status update miss its row (the "No record updated for
    app_id" log) and the dashboard report the app as unlimited.
    """

    def _apply(self, target, app_id):
        b = _balancer()
        b.all_limits = balancer_mod.LimitRegistry()
        b.is_running = True
        b._stressed_disks = lambda: []
        b.io_ctl.set_disk_io_throttle = lambda *a, **kw: True
        b.resource_monitor = mock.Mock(**{'get_total_memory.return_value': 16 * 1024 ** 3})
        rates = {'disk_io_rate': {'read': 50, 'write': 20,
                                  'read_iops': 2000, 'write_iops': 1000}}
        with mock.patch.object(balancer_mod.app_utils, 'update_app_status') as status, \
             mock.patch.object(balancer_mod.app_utils, 'callback_manager'):
            b._apply_resource_limits(target, app_id, rates, True, is_disk_io_stressed=True)
        return b.all_limits.apps, status

    def test_resolved_app_is_recorded_under_its_public_id(self):
        target = {'process': {'name': 'fio'}, 'app': {'id': 'lo-app'},
                  'public_app_id': 'lo-app', 'extra_cgroups': ['lo-helper.scope']}
        apps, status = self._apply(target, 'lo-io.scope')
        entry = apps['lo-io.scope']          # keyed by cgroup: that is what io.max needs
        self.assertEqual(entry.public_app_id, 'lo-app')
        self.assertEqual(entry.cgroups, ['lo-io.scope', 'lo-helper.scope'])
        status.assert_called_once_with('lo-app', 'limited')

    def test_unresolved_app_keeps_the_id_it_came_with(self):
        apps, status = self._apply({'process': {'name': 'fio'}}, 'hi-io.scope')
        self.assertEqual(apps['hi-io.scope'].public_app_id, 'hi-io.scope')
        status.assert_called_once_with('hi-io.scope', 'limited')


if __name__ == "__main__":
    unittest.main(verbosity=2)
