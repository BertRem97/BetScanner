#!/bin/bash

SERVERS=(
"/etc/openvpn/be-bru.prod.surfshark.comsurfshark_openvpn_udp.ovpn"
"/etc/openvpn/de-fra.prod.surfshark.comsurfshark_openvpn_udp.ovpn"
"/etc/openvpn/fr-par.prod.surfshark.comsurfshark_openvpn_udp.ovpn"
"/etc/openvpn/nl-ams.prod.surfshark.comsurfshark_openvpn_udp.ovpn"
)

SERVER=${SERVERS[$RANDOM % ${#SERVERS[@]}]}

echo "Switching to $SERVER"

sudo pkill openvpn

while pgrep openvpn >/dev/null; do
    sleep 1
done

sudo openvpn \
    --config "$SERVER" \
    --daemon

echo "Waiting for VPN..."

for i in {1..60}; do

    if ip addr show tun0 >/dev/null 2>&1 &&
       ping -c1 -W2 1.1.1.1 >/dev/null 2>&1 &&
       getent hosts api.telegram.org >/dev/null 2>&1 &&
       getent hosts api.oddspapi.io >/dev/null 2>&1
    then
        break
    fi

    sleep 2
done

IP=$(curl -s --max-time 5 ifconfig.me)

echo "New IP: $IP"

