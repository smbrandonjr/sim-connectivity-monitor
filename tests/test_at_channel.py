import pytest

from sim_monitor.modem.at_channel import ATChannel, ATCommandError
from sim_monitor.modem.driver_base import ModemError


class FakeSerial:
    """Scriptable serial port: maps written commands to response lines.

    `prebuffer` seeds lines as if the modem emitted them unsolicited before any
    command (URC capture testing); `in_waiting` reflects buffered bytes."""

    def __init__(self, responses, echo=False, prebuffer=None):
        self.responses = responses  # command -> list of lines (str)
        self.echo = echo
        self.buffer: list[bytes] = [(s + "\r\n").encode() for s in (prebuffer or [])]
        self.is_open = True
        self.written: list[str] = []

    def write(self, data: bytes):
        command = data.decode().strip()
        self.written.append(command)
        lines = []
        if self.echo:
            lines.append(command)
        lines += self.responses.get(command, ["ERROR"])
        self.buffer += [(line + "\r\n").encode() for line in lines]

    def readline(self):
        return self.buffer.pop(0) if self.buffer else b""

    @property
    def in_waiting(self):
        return sum(len(b) for b in self.buffer)

    def reset_input_buffer(self):
        pass

    def close(self):
        self.is_open = False


def make_channel(responses, echo=False, timeout=0.3):
    fake = FakeSerial(responses, echo=echo)
    channel = ATChannel(
        "COM_FAKE", timeout=timeout, serial_factory=lambda *a, **k: fake
    )
    return channel, fake


def test_execute_returns_payload_without_ok():
    channel, _ = make_channel({"AT+CSQ": ["+CSQ: 18,99", "OK"]})
    assert channel.execute("AT+CSQ") == ["+CSQ: 18,99"]


def test_echo_lines_are_skipped():
    channel, _ = make_channel({"AT+CSQ": ["+CSQ: 18,99", "OK"]}, echo=True)
    assert channel.execute("AT+CSQ") == ["+CSQ: 18,99"]


def test_multiline_payload():
    channel, _ = make_channel(
        {"AT+CGDCONT?": ['+CGDCONT: 1,"IP","a"', '+CGDCONT: 2,"IP","b"', "OK"]}
    )
    assert len(channel.execute("AT+CGDCONT?")) == 2


def test_cme_error_raises_command_error():
    channel, _ = make_channel({"AT+CPIN?": ["+CME ERROR: 10"]})
    with pytest.raises(ATCommandError, match="CME ERROR: 10"):
        channel.execute("AT+CPIN?")


def test_plain_error_raises():
    channel, _ = make_channel({})
    with pytest.raises(ATCommandError):
        channel.execute("AT+NOPE")


def test_timeout_raises_and_closes_port():
    channel, fake = make_channel({"AT+SLOW": []})  # no terminal line ever arrives
    with pytest.raises(ModemError, match="timeout"):
        channel.execute("AT+SLOW", timeout=0.2)
    assert fake.is_open is False


def test_urcs_buffered_before_command_are_dispatched():
    fake = FakeSerial({"AT+CSQ": ["+CSQ: 18,99", "OK"]}, prebuffer=['+CMTI: "ME",3'])
    captured = []
    channel = ATChannel("COM_FAKE", timeout=0.3, serial_factory=lambda *a, **k: fake)
    channel.set_urc_handler(captured.append)
    payload = channel.execute("AT+CSQ")
    assert payload == ["+CSQ: 18,99"]          # response still clean
    assert captured == ['+CMTI: "ME",3']        # URC captured, not discarded


def test_drain_urcs_dispatches_without_a_command():
    fake = FakeSerial({}, prebuffer=["+QSIMSTAT: 1,1", '+CMTI: "ME",1'])
    captured = []
    channel = ATChannel("COM_FAKE", serial_factory=lambda *a, **k: fake)
    channel.set_urc_handler(captured.append)
    channel.open()
    channel.drain_urcs()
    assert captured == ["+QSIMSTAT: 1,1", '+CMTI: "ME",1']


def test_async_urc_interleaved_during_command_is_diverted():
    # A +CMTI arrives mixed into a command's reply lines.
    fake = FakeSerial({"AT+CSQ": ['+CMTI: "ME",5', "+CSQ: 18,99", "OK"]})
    captured = []
    channel = ATChannel("COM_FAKE", timeout=0.3, serial_factory=lambda *a, **k: fake)
    channel.set_urc_handler(captured.append)
    payload = channel.execute("AT+CSQ")
    assert payload == ["+CSQ: 18,99"]
    assert captured == ['+CMTI: "ME",5']


def test_drain_urcs_noop_when_closed():
    channel = ATChannel("COM_FAKE", serial_factory=lambda *a, **k: FakeSerial({}))
    channel.drain_urcs()  # never opened — must not raise


def test_reopens_after_close():
    opens = []

    def factory(*args, **kwargs):
        fake = FakeSerial({"AT": ["OK"]})
        opens.append(fake)
        return fake

    channel = ATChannel("COM_FAKE", timeout=0.3, serial_factory=factory)
    channel.execute("AT")
    channel.close()
    channel.execute("AT")
    assert len(opens) == 2
