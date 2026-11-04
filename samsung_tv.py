"""Send remote-control keys to a Samsung Smart TV (2016 and newer) over Wi-Fi.

Plain-socket WebSocket client with no third-party packages, so the same file
can later move to the CircuitPython board.

How the TV works:
  * Port 8002 (wss, TLS): newer sets. First connect shows "Allow?" on the TV
    screen. Accept it once; the TV then hands back a token. Save it and send it
    on every later connect so the prompt never returns.
  * Port 8001 (ws, no TLS): some 2016 to 2018 sets. No token needed.
"""
import base64
import json
import os
import socket
import ssl
import time


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("connection closed")
        buf += chunk
    return buf


def _read_frame(sock):
    """Return (opcode, payload bytes) for one WebSocket frame."""
    b1, b2 = _recv_exact(sock, 2)
    opcode = b1 & 0x0F
    length = b2 & 0x7F
    if length == 126:
        length = int.from_bytes(_recv_exact(sock, 2), "big")
    elif length == 127:
        length = int.from_bytes(_recv_exact(sock, 8), "big")
    if b2 & 0x80:  # servers should not mask, but be safe
        mask = _recv_exact(sock, 4)
        data = bytearray(_recv_exact(sock, length))
        for i in range(length):
            data[i] ^= mask[i % 4]
        return opcode, bytes(data)
    return opcode, _recv_exact(sock, length)


def _send_frame(sock, opcode, payload=b""):
    """Clients must mask every frame they send."""
    head = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head.append(0x80 | n)
    elif n < 65536:
        head.append(0x80 | 126)
        head += n.to_bytes(2, "big")
    else:
        head.append(0x80 | 127)
        head += n.to_bytes(8, "big")
    mask = os.urandom(4)
    head += mask
    body = bytearray(payload)
    for i in range(n):
        body[i] ^= mask[i % 4]
    sock.sendall(bytes(head) + bytes(body))


def send_key(host, key, token=None, port=8002, name="GrandmaRemote", timeout=8, verbose=True):
    """Send one key (e.g. "KEY_VOLUP") to the TV.

    Returns the token the TV reported (or the one passed in), so the caller can
    save it. Raises OSError / ValueError with a readable message on failure.
    """
    use_tls = port == 8002
    raw = socket.create_connection((host, port), timeout=timeout)
    if use_tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # the TV's certificate is self-signed
        sock = ctx.wrap_socket(raw, server_hostname=host)
    else:
        sock = raw
    sock.settimeout(timeout)

    try:
        path = "/api/v2/channels/samsung.remote.control?name=" + base64.b64encode(name.encode()).decode()
        if token:
            path += "&token=" + token
        ws_key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((
            "GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, ws_key)
        ).encode())

        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(1)
            if not chunk:
                raise OSError("TV closed the connection during handshake")
            head += chunk
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101 " not in status:
            raise OSError("handshake refused: " + status)

        new_token = token
        deadline = time.time() + (35 if not token else timeout)  # first time: time to press Allow
        connected = False
        sock.settimeout(1.0)
        while time.time() < deadline and not connected:
            try:
                opcode, payload = _read_frame(sock)
            except socket.timeout:
                continue
            if opcode == 9:  # ping
                _send_frame(sock, 10, payload)
                continue
            if opcode == 8:
                raise OSError("TV closed the connection")
            if opcode != 1:
                continue
            msg = json.loads(payload.decode())
            if verbose:
                print("  TV says:", msg.get("event"), msg.get("data", ""))
            event = msg.get("event")
            if event == "ms.channel.connect":
                connected = True
                new_token = (msg.get("data") or {}).get("token") or token
            elif event == "ms.channel.unauthorized":
                raise OSError("TV said unauthorized (wrong or missing token, or access was denied)")
            elif event == "ms.error":
                raise OSError("TV error: %s" % msg.get("data"))
        if not connected:
            raise OSError("no connect event: did you press Allow on the TV?")

        cmd = {"method": "ms.remote.control", "params": {
            "Cmd": "Click", "DataOfCmd": key, "Option": "false", "TypeOfRemote": "SendRemoteKey"}}
        _send_frame(sock, 1, json.dumps(cmd).encode())
        time.sleep(0.3)  # let the TV take it before we hang up
        try:
            _send_frame(sock, 8, b"")
        except OSError:
            pass
        return new_token
    finally:
        try:
            sock.close()
        except OSError:
            pass