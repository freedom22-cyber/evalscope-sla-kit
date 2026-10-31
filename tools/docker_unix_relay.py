#!/usr/bin/env python3
"""Unix-socket relay: expose host dockerd to the ais-bench container.

The container shares /mnt/sfs_turbo with the host, so a unix socket created
at a path under that mount is visible from both sides.  docker-py's exec path
uses an HTTP 101 hijack and then reads the raw socket, so a TCP proxy loses
bytes on segment boundaries -- a unix socket preserves write boundaries.

Usage: python3 docker_unix_relay.py <relay_socket_path>
"""
import os
import socket
import sys
import threading

RELAY_PATH = sys.argv[1]
TARGET = "/var/run/docker.sock"


def pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle(conn: socket.socket) -> None:
    try:
        up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        up.connect(TARGET)
    except OSError:
        conn.close()
        return
    t1 = threading.Thread(target=pump, args=(conn, up), daemon=True)
    t2 = threading.Thread(target=pump, args=(up, conn), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    for s in (conn, up):
        try:
            s.close()
        except OSError:
            pass


def main() -> None:
    os.makedirs(os.path.dirname(RELAY_PATH), exist_ok=True)
    if os.path.exists(RELAY_PATH):
        os.unlink(RELAY_PATH)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(RELAY_PATH)
    os.chmod(RELAY_PATH, 0o666)
    srv.listen(128)
    print(f"relay {RELAY_PATH} -> {TARGET}", flush=True)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
