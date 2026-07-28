from __future__ import annotations

import socket
import time
from dataclasses import dataclass


class AvalonClientError(RuntimeError):
    """Raised when the Avalon TCP API cannot be reached or returns no data."""


class AvalonCommandRejected(RuntimeError):
    """Raised when the Avalon API returns STATUS=E for a command."""


@dataclass(frozen=True)
class AvalonCommands:
    version: str = "version"
    summary: str = "summary"
    stats: str = "stats"
    eco: str = "ascset|0,workmode,set,0"
    standard: str = "ascset|0,workmode,set,1"
    super: str = "ascset|0,workmode,set,2"
    softoff_legacy: str = "ascset|0,softoff,1:0"

    def softoff(self, unix_timestamp: int) -> str:
        return f"ascset|0,softoff,1:{unix_timestamp}"

    def softon(self, unix_timestamp: int) -> str:
        return f"ascset|0,softon,1:{unix_timestamp}"


class AvalonClient:
    def __init__(self, host: str, port: int = 4028, timeout_seconds: float = 5.0):
        self.host = host
        self.port = port
        self.timeout_seconds = timeout_seconds
        self.commands = AvalonCommands()

    def send_command(self, command: str) -> str:
        """Send one raw TCP text command to the Avalon API and return raw text."""
        payload = command.encode("ascii")
        chunks: list[bytes] = []

        try:
            with socket.create_connection(
                (self.host, self.port),
                timeout=self.timeout_seconds,
            ) as sock:
                sock.settimeout(self.timeout_seconds)
                sock.sendall(payload)

                while True:
                    try:
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    chunks.append(chunk)
        except OSError as exc:
            raise AvalonClientError(
                f"Could not send raw TCP command to {self.host}:{self.port}: {exc}"
            ) from exc

        if not chunks:
            return ""
        return b"".join(chunks).decode("utf-8", errors="replace")

    def get_version(self) -> str:
        return self.send_command(self.commands.version)

    def get_summary(self) -> str:
        return self.send_command(self.commands.summary)

    def get_stats(self) -> str:
        return self.send_command(self.commands.stats)

    def set_eco(self) -> str:
        return self.send_command(self.commands.eco)

    def set_super(self) -> str:
        return self.send_command(self.commands.super)

    def set_standard(self) -> str:
        return self.send_command(self.commands.standard)

    def softoff(self) -> str:
        timestamp = int(time.time()) + 8
        return self.send_command(self.commands.softoff(timestamp))

    def softon(self, delay_seconds: int = 8) -> str:
        timestamp = int(time.time()) + delay_seconds
        return self.send_command(self.commands.softon(timestamp))

    @staticmethod
    def response_is_error(response: str) -> bool:
        return response.lstrip().startswith("STATUS=E")
