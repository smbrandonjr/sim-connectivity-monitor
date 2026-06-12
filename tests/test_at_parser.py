import pytest

from sim_monitor.modem.at_parser import (
    ActualPdpContext,
    ATParseError,
    parse_cgdcont,
    parse_cgmi,
    parse_cops,
    parse_cpin,
    parse_csq,
    parse_iccid,
    parse_imsi,
    pdp_type_to_at,
)


class TestIccid:
    def test_quectel_qccid(self):
        assert parse_iccid(["+QCCID: 8944500612345678901"]) == "8944500612345678901"

    def test_simcom_ccid_with_filler(self):
        assert parse_iccid(["+CCID: 8944500612345678901F"]) == "8944500612345678901F"

    def test_telit_hash_ccid(self):
        assert parse_iccid(["#CCID: 8944500612345678901"]) == "8944500612345678901"

    def test_bare_number(self):
        assert parse_iccid(["", "8944500612345678901"]) == "8944500612345678901"

    def test_quoted(self):
        assert parse_iccid(['+CCID: "8944500612345678901"']) == "8944500612345678901"

    def test_missing_raises(self):
        with pytest.raises(ATParseError):
            parse_iccid(["+CME ERROR: 10"])


class TestImsi:
    def test_bare(self):
        assert parse_imsi(["234500000000001"]) == "234500000000001"

    def test_missing(self):
        with pytest.raises(ATParseError):
            parse_imsi(["ERROR"])


class TestCsq:
    def test_normal(self):
        sq = parse_csq(["+CSQ: 18,99"])
        assert sq.rssi_dbm == -113 + 36
        assert sq.percent == 58

    def test_unknown_99(self):
        sq = parse_csq(["+CSQ: 99,99"])
        assert sq.rssi_dbm is None
        assert sq.percent is None

    def test_max(self):
        assert parse_csq(["+CSQ: 31,0"]).rssi_dbm == -51

    def test_missing(self):
        with pytest.raises(ATParseError):
            parse_csq(["OK"])


class TestCops:
    def test_with_operator(self):
        assert parse_cops(['+COPS: 0,0,"Hologram",7']) == "Hologram"

    def test_not_registered(self):
        assert parse_cops(["+COPS: 0"]) is None

    def test_missing(self):
        with pytest.raises(ATParseError):
            parse_cops([""])


class TestCpin:
    def test_ready(self):
        assert parse_cpin(["+CPIN: READY"]) == "READY"

    def test_sim_pin(self):
        assert parse_cpin(["+CPIN: SIM PIN"]) == "SIM PIN"


class TestCgmi:
    def test_quectel(self):
        assert parse_cgmi(["Quectel"]) == "Quectel"

    def test_skips_echo(self):
        assert parse_cgmi(["AT+CGMI", "SIMCOM INCORPORATED"]) == "SIMCOM INCORPORATED"


class TestCgdcont:
    def test_multiple_contexts(self):
        lines = [
            '+CGDCONT: 1,"IP","hologram","0.0.0.0",0,0',
            '+CGDCONT: 2,"IPV4V6","ims","",0,0',
            '+CGDCONT: 8,"IPV6","internet.v6","",0,0',
        ]
        result = parse_cgdcont(lines)
        assert result == [
            ActualPdpContext(1, "IPv4", "hologram"),
            ActualPdpContext(2, "IPv4v6", "ims"),
            ActualPdpContext(8, "IPv6", "internet.v6"),
        ]

    def test_empty_response_means_no_contexts(self):
        assert parse_cgdcont([]) == []

    def test_unknown_type_passthrough(self):
        result = parse_cgdcont(['+CGDCONT: 1,"PPP","x","",0,0'])
        assert result[0].pdp_type == "PPP"


def test_pdp_type_roundtrip():
    assert pdp_type_to_at("IPv4") == "IP"
    assert pdp_type_to_at("IPv6") == "IPV6"
    assert pdp_type_to_at("IPv4v6") == "IPV4V6"
