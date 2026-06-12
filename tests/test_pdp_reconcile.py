from sim_monitor.config.schema import PdpContext
from sim_monitor.modem.at_parser import ActualPdpContext
from sim_monitor.modem.pdp_reconcile import DefineContext, DeleteContext, reconcile


def desired(cid, apn, pdp_type="IPv4", bearer=False):
    return PdpContext(cid=cid, apn=apn, pdp_type=pdp_type, bearer=bearer)


def actual(cid, apn, pdp_type="IPv4"):
    return ActualPdpContext(cid=cid, pdp_type=pdp_type, apn=apn)


class TestReconcile:
    def test_already_correct_no_actions(self):
        assert reconcile([actual(1, "hologram")], [desired(1, "hologram", bearer=True)]) == []

    def test_apn_case_insensitive(self):
        assert reconcile([actual(1, "HOLOGRAM")], [desired(1, "hologram", bearer=True)]) == []

    def test_empty_modem_defines_all(self):
        d1, d2 = desired(1, "a", bearer=True), desired(2, "b")
        assert reconcile([], [d1, d2]) == [DefineContext(d1), DefineContext(d2)]

    def test_stray_firmware_contexts_deleted(self):
        d = desired(1, "hologram", bearer=True)
        actions = reconcile(
            [actual(1, "hologram"), actual(8, "ims", "IPv4v6"), actual(16, "vzwadmin")],
            [d],
        )
        assert actions == [DeleteContext(8), DeleteContext(16)]

    def test_mismatched_apn_redefined_in_place(self):
        d = desired(1, "hologram", bearer=True)
        assert reconcile([actual(1, "internet")], [d]) == [DefineContext(d)]

    def test_mismatched_pdp_type_redefined(self):
        d = desired(1, "hologram", "IPv4v6", bearer=True)
        assert reconcile([actual(1, "hologram", "IPv4")], [d]) == [DefineContext(d)]

    def test_deletes_come_before_defines(self):
        d = [desired(1, "a", bearer=True), desired(2, "b"), desired(3, "c")]
        actions = reconcile([actual(2, "wrong"), actual(7, "stray")], d)
        assert actions == [
            DeleteContext(7),
            DefineContext(d[0]),
            DefineContext(d[1]),
            DefineContext(d[2]),
        ]

    def test_three_context_hologram_profile(self):
        # The Hologram profile type that needs exactly three specific contexts.
        d = [
            desired(1, "hologram", "IPv4v6", bearer=True),
            desired(2, "hologram.special"),
            desired(3, "ims", "IPv4v6"),
        ]
        # Modem booted with its own ideas:
        a = [actual(1, "internet"), actual(4, "vzwims", "IPv4v6")]
        actions = reconcile(a, d)
        assert actions == [
            DeleteContext(4),
            DefineContext(d[0]),
            DefineContext(d[1]),
            DefineContext(d[2]),
        ]
