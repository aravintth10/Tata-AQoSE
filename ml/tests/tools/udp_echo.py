"""Echoes every datagram back. Listens on IPv4 and IPv6."""
import socket
import sys

sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
sock.bind(("::", int(sys.argv[1])))
while True:
    data, addr = sock.recvfrom(2000)
    sock.sendto(data, addr)
