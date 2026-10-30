"""Sends datagrams from a fixed port and waits for each echo.

    udp_send.py <server> <server port> <own port> <count> [tos]
"""
import socket
import sys
import time

server, port, own, count = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
family = socket.AF_INET6 if ":" in server else socket.AF_INET
sock = socket.socket(family, socket.SOCK_DGRAM)
if len(sys.argv) > 5:
    if family == socket.AF_INET:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, int(sys.argv[5]))
    else:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_TCLASS, int(sys.argv[5]))
sock.bind(("", own))
sock.settimeout(0.2)
for _ in range(count):
    sock.sendto(bytes(200), (server, port))
    try:
        sock.recv(2000)
    except OSError:
        pass
    time.sleep(0.004)
