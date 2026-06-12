import pytest

from sim_monitor.config.schema import PdpContext
from sim_monitor.modem.at_channel import ATCommandError
from sim_monitor.modem.at_driver import ATModemDriver
from sim_monitor.modem.drivers import QuectelDriver, SimcomDriver, TelitDriver


class ScriptedChannel:
    """In-memory AT channel: exact command -> payload lines or exception."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.executed: list[str] = []
        self.closed = False

    def execute(self, command, timeout=None):
        self.executed.append(command)
        result = self.responses.get(command)
        if result is None:
            raise ATCommandError(f"{command!r} -> ERROR")
        if isinstance(result, Exception):
            raise result
        return result

    def open(self):
        pass

    def close(self):
        self.closed = True


IDENTITY = {
    "AT+CGMI": ["Quectel"],
    "AT+CGMM": ["EC25"],
    "AT+CGSN": ["490154203237518"],
}


class TestIdentityAndSim:
    def test_identity(self):
        driver = QuectelDriver(ScriptedChannel(IDENTITY))
        identity = driver.get_identity()
        assert (identity.vendor, identity.model, identity.imei) == (
            "Quectel", "EC25", "490154203237518",
        )

    def test_sim_present_quectel(self):
        driver = QuectelDriver(
            ScriptedChannel(
                {
                    "AT+CPIN?": ["+CPIN: READY"],
                    "AT+QCCID": ["+QCCID: 8944500612345678901"],
                    "AT+CIMI": ["234500000000001"],
                }
            )
        )
        sim = driver.get_sim_status()
        assert sim.present and sim.iccid == "8944500612345678901"

    def test_sim_absent_cme10(self):
        channel = ScriptedChannel({"AT+CPIN?": ATCommandError("'AT+CPIN?' -> +CME ERROR: 10")})
        sim = QuectelDriver(channel).get_sim_status()
        assert sim.present is False
        assert "no SIM" in sim.detail

    def test_sim_locked_reports_detail(self):
        driver = QuectelDriver(ScriptedChannel({"AT+CPIN?": ["+CPIN: SIM PIN"]}))
        sim = driver.get_sim_status()
        assert sim.present is False
        assert "SIM PIN" in sim.detail

    def test_simcom_iccid_command(self):
        driver = SimcomDriver(
            ScriptedChannel(
                {
                    "AT+CPIN?": ["+CPIN: READY"],
                    "AT+CICCID": ["+ICCID: 8944500612345678901"],
                    "AT+CIMI": ["234500000000001"],
                }
            )
        )
        assert driver.get_sim_status().iccid == "8944500612345678901"

    def test_telit_iccid_command(self):
        driver = TelitDriver(
            ScriptedChannel(
                {
                    "AT+CPIN?": ["+CPIN: READY"],
                    "AT#CCID": ["#CCID: 8944500612345678901"],
                    "AT+CIMI": ["234500000000001"],
                }
            )
        )
        assert driver.get_sim_status().iccid == "8944500612345678901"


class TestPdpOperations:
    def test_define_without_auth(self):
        channel = ScriptedChannel({'AT+CGDCONT=1,"IP","hologram"': []})
        QuectelDriver(channel).define_pdp_context(
            PdpContext(cid=1, apn="hologram", bearer=True)
        )
        assert channel.executed == ['AT+CGDCONT=1,"IP","hologram"']

    def test_define_with_pap_auth_uses_cgauth(self):
        channel = ScriptedChannel(
            {
                'AT+CGDCONT=2,"IPV4V6","special"': [],
                'AT+CGAUTH=2,1,"user","pass"': [],
            }
        )
        QuectelDriver(channel).define_pdp_context(
            PdpContext(
                cid=2, apn="special", pdp_type="IPv4v6",
                auth="pap", username="user", password="pass",
            )
        )
        assert 'AT+CGAUTH=2,1,"user","pass"' in channel.executed

    def test_telit_auth_uses_pdpauth(self):
        channel = ScriptedChannel(
            {
                'AT+CGDCONT=2,"IP","special"': [],
                'AT#PDPAUTH=2,2,"user","pass"': [],
            }
        )
        TelitDriver(channel).define_pdp_context(
            PdpContext(cid=2, apn="special", auth="chap", username="user", password="pass")
        )
        assert 'AT#PDPAUTH=2,2,"user","pass"' in channel.executed

    def test_delete(self):
        channel = ScriptedChannel({"AT+CGDCONT=8": []})
        QuectelDriver(channel).delete_pdp_context(8)
        assert channel.executed == ["AT+CGDCONT=8"]

    def test_get_contexts_error_means_empty(self):
        channel = ScriptedChannel()  # AT+CGDCONT? -> ERROR
        assert QuectelDriver(channel).get_pdp_contexts() == []


class TestControl:
    @pytest.mark.parametrize(
        "driver_cls,reset_cmd",
        [
            (QuectelDriver, "AT+CFUN=1,1"),
            (SimcomDriver, "AT+CRESET"),
            (TelitDriver, "AT#REBOOT"),
            (ATModemDriver, "AT+CFUN=1,1"),
        ],
    )
    def test_full_reset_command_and_close(self, driver_cls, reset_cmd):
        channel = ScriptedChannel({reset_cmd: ["OK"]})
        driver_cls(channel).full_reset()
        assert channel.executed == [reset_cmd]
        assert channel.closed

    def test_full_reset_tolerates_port_death(self):
        channel = ScriptedChannel()  # reset command -> ERROR (port died)
        QuectelDriver(channel).full_reset()  # must not raise
        assert channel.closed

    def test_airplane_mode(self):
        channel = ScriptedChannel({"AT+CFUN=4": [], "AT+CFUN=1": []})
        driver = QuectelDriver(channel)
        driver.set_airplane(True)
        driver.set_airplane(False)
        assert channel.executed == ["AT+CFUN=4", "AT+CFUN=1"]

    def test_init_commands(self):
        channel = ScriptedChannel({'AT+QCFG="nwscanmode",0': []})
        QuectelDriver(channel).run_init_commands(['AT+QCFG="nwscanmode",0'])
        assert channel.executed == ['AT+QCFG="nwscanmode",0']
