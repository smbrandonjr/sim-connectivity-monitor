import pytest

from sim_monitor.modem.at_channel import ATChannel, ATCommandError
from sim_monitor.modem.driver_base import ModemError


class FakeSerial:
    """Scriptable serial port: maps written commands to response lines."""

    def __init__(self, responses, echo=False):
        self.responses = responses  # command -> list of lines (str)
        self.echo = echo
        self.buffer: list[bytes] = []
        self.is_open = True
        self.written: list[str] = []

    def write(self, data: bytes):
        command = data.decode().strip()
        self.written.append(command)
        lines = []
        if self.echo:
            lines.append(command)
        lines += self.responses.get(command, ["ERROR"])
        self.buffer = [(line + "\r\n").encode() for line in lines]

    def readline(self):
        return self.buffer.pop(0) if self.buffer else b""

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
