import socket
import threading

import pytest

from avalon_q_scheduler.avalon_client import AvalonClient, AvalonClientError


def run_one_shot_server(response: bytes):
    ready = threading.Event()
    result = {}

    def server():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            result["port"] = sock.getsockname()[1]
            ready.set()
            conn, _ = sock.accept()
            with conn:
                result["command"] = conn.recv(4096)
                conn.sendall(response)

    thread = threading.Thread(target=server, daemon=True)
    thread.start()
    ready.wait(timeout=2)
    return result, thread


def test_send_command_uses_raw_tcp_text():
    server, thread = run_one_shot_server(b"OK=1")
    client = AvalonClient("127.0.0.1", server["port"], timeout_seconds=1)

    response = client.send_command("version")
    thread.join(timeout=2)

    assert server["command"] == b"version"
    assert b"HTTP" not in server["command"]
    assert response == "OK=1"


def test_unreachable_miner_raises_clean_error():
    client = AvalonClient("127.0.0.1", 1, timeout_seconds=0.1)

    with pytest.raises(AvalonClientError):
        client.send_command("version")


def test_mode_commands_are_available_without_contacting_miner():
    client = AvalonClient("192.0.2.1")

    assert client.commands.eco == "ascset|0,workmode,set,0"
    assert client.commands.standard == "ascset|0,workmode,set,1"
    assert client.commands.super == "ascset|0,workmode,set,2"
    assert client.commands.softoff_legacy == "ascset|0,softoff,1:0"
    assert client.commands.softoff(123) == "ascset|0,softoff,1:123"
    assert client.commands.softon(123) == "ascset|0,softon,1:123"


def test_status_e_response_is_error():
    assert AvalonClient.response_is_error("STATUS=E,Code=120,Msg=failed|")
    assert not AvalonClient.response_is_error("STATUS=S,Code=119,Msg=OK|")
