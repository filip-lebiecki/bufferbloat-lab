# SQM for a MikroTik home router (RouterOS 7.1+) — cake in both directions.
# Paste into the terminal or import with: /import mikrotik-home-router.rsc
#
# THE TWO STRUCTURAL RULES THIS CONFIG IS BUILT ON:
#   1. Queue trees are parented to GLOBAL, not to an interface.
#   2. Direction is supplied by MANGLE PACKET MARKS, because a global tree
#      sees both directions at once and cannot tell them apart by itself.
#      in-interface=<wan> = download, out-interface=<wan> = upload.
#      No marks => no shaping. They are load-bearing, not decoration.
#      And they are written TWICE — /ip firewall mangle AND /ipv6 firewall
#      mangle — because the IPv4 table does not see IPv6 traffic at all.
#
# EDIT THESE before running:
#   - ether1  -> your WAN interface (every occurrence, mangle rules included)
#   - 45M/90M -> ~90% of your MEASURED up/down rates. Measure the line
#     UNSHAPED first; do not trust the advertised tier. 90% is the working
#     figure — on a real 490 Mbps line, 94% still graded B while 90% got A+.
#     The rate goes on max-limit in the /queue tree section ONLY;
#     cake-bandwidth stays 0. See note 1 at the bottom.
#   - cake-overhead-scheme: docsis (cable) | pppoe-vcmux (DSL) | ethernet
#
# ############################################################################
# STEP 0 — FASTTRACK MUST BE OFF. Do this FIRST or nothing below will work.
# ############################################################################
# This config classifies traffic with packet marks, and fasttracked packets
# never get marks. With FastTrack enabled the queues see no traffic and shape
# nothing — silently, with no error anywhere. Stock RouterOS SHIPS a
# fasttrack rule in its default firewall, so assume you have one:
#
#   /ip firewall filter print where action=fasttrack-connection
#   /ip firewall filter disable [find action=fasttrack-connection]
#
# Confirm it is really off (both must be true):
#   /ip settings print        -> ipv4-fasttrack-active: no
#   /ip firewall filter print where action=fasttrack-connection   -> empty
#
# Cost: FastTrack existed to save CPU, so expect higher CPU per packet. That
# is simply the price of shaping. If you cannot afford it, see the
# interface-parented alternative in note 3 — it keeps FastTrack, but does not
# work on all hardware.

# --- queue types -------------------------------------------------------------
# cake-bandwidth=0 is deliberate — the rate lives on max-limit in the tree
# below, because cake-bandwidth does not shape. See note 1.
# dual-srchost/dual-dsthost + cake-nat=yes = per-LAN-machine fairness.
# ack-filter on upload only: it thins redundant ACKs, which helps on
# asymmetric links and does nothing useful in the download direction.
/queue type
add name=cake-up kind=cake cake-bandwidth=0 cake-diffserv=diffserv3 \
    cake-flowmode=dual-srchost cake-nat=yes cake-ack-filter=filter \
    cake-rtt-scheme=internet cake-overhead-scheme=ethernet
add name=cake-down kind=cake cake-bandwidth=0 cake-diffserv=diffserv3 \
    cake-flowmode=dual-dsthost cake-nat=yes \
    cake-rtt-scheme=internet cake-overhead-scheme=ethernet

# --- direction classifiers (IPv4) --------------------------------------------
# parent=global trees cannot tell direction by themselves, so mangle supplies
# it: arriving on the WAN = download, leaving via the WAN = upload.
# passthrough=no stops rule evaluation once a packet is classified.
/ip firewall mangle
add chain=forward action=mark-packet new-packet-mark=wan-dl passthrough=no \
    in-interface=ether1 comment="CAKE download classify"
add chain=forward action=mark-packet new-packet-mark=wan-ul passthrough=no \
    out-interface=ether1 comment="CAKE upload classify"

# --- direction classifiers (IPv6) --------------------------------------------
# REQUIRED, not optional. /ip firewall mangle does not see IPv6 at all, so
# without these your entire IPv6 traffic is completely unshaped in both
# directions — and IPv6 is what most big sites and speedtests actually use.
# (Native IPv6 on the WAN only. IPv6 riding a 6in4/HE tunnel is encapsulated
#  and will NOT match these rules — see note 4.)
/ipv6 firewall mangle
add chain=forward action=mark-packet new-packet-mark=wan-dl passthrough=no \
    in-interface=ether1 comment="CAKE download classify"
add chain=forward action=mark-packet new-packet-mark=wan-ul passthrough=no \
    out-interface=ether1 comment="CAKE upload classify"

# --- queue trees -------------------------------------------------------------
# max-limit is what actually enforces the rate, and it is the ONLY place the
# rate appears. Both IPv4 and IPv6 marks feed the same two trees.
/queue tree
add name=sqm-download parent=global packet-mark=wan-dl queue=cake-down max-limit=90M
add name=sqm-upload   parent=global packet-mark=wan-ul queue=cake-up   max-limit=45M

# ############################################################################
# VERIFYING
# ############################################################################
# /queue tree print stats during a speedtest. Climbing bytes only prove
# CLASSIFICATION — check that the RATE CAPS at your configured value. Then run
# a bufferbloat test; libreqos.io is the better one (it adds a bidirectional
# phase that waveform.com lacks).
#
# Do NOT judge cake by the queue counters:
#   - dropped=0 always, working or not — the counter is not populated.
#   - queued-bytes shows the BULK flow's backlog (~2 MB under a saturated
#     download). That is not added latency; cake's flow isolation keeps
#     interactive traffic out of that queue. Dividing it by the rate to
#     "compute" latency gives a scary number that is simply wrong.
# Latency measured end to end is the only signal that means anything.
#
# ############################################################################
# NOTES
# ############################################################################
# 1. max-limit on the queue TREE enforces the rate; cake-bandwidth on the
#    queue TYPE does not shape at all. Measured on a real RB5009 running
#    RouterOS 7.23.2 (see rb5009-cake.rsc):
#      cake-bandwidth=420M, no max-limit -> 490 Mbps  (ignored entirely)
#      cake-bandwidth=50M,  no max-limit -> 206 Mbps  (4x over)
#      max-limit=440M, cake-bandwidth=0  -> caps at 435. Waveform A+, libreqos A
#    max-limit=440M with cake-bandwidth=450M measured identically to
#    cake-bandwidth=0, confirming cake-bandwidth contributes nothing.
#    HTB shapes; cake does AQM, flow isolation and per-host fairness — which
#    is all it was ever contributing here.
#
# 2. Hardware reality check: cake runs on the CPU, and FastTrack is off. A
#    hEX/hAP handles ~100-200 Mbit of cake; gigabit lines need RB5009/CCR
#    class or lower shaped rates. An RB5009 sat at 37-50% CPU shaping
#    440 Mbps on one direction.
#
# 3. ALTERNATIVE: interface-parented trees, which keep FastTrack enabled.
#    FastTrack skips simple queues and parent=global trees, but NOT queues
#    attached to an interface:
#      /queue tree
#      add name=sqm-upload   parent=ether1 packet-mark=no-mark queue=cake-up   max-limit=45M
#      add name=sqm-download parent=bridge packet-mark=no-mark queue=cake-down max-limit=90M
#    (packet-mark=no-mark is required — fasttracked packets carry no marks —
#     and no mangle rules are needed at all, including for IPv6, because an
#     interface queue sees every packet regardless of protocol.)
#    THE CATCH: this does not work on all hardware, and it fails SILENTLY. On
#    an RB5009, trees parented to the WAN or to a trunk VLAN reported
#    ACTIVE-QUEUE=queue-tree and moved EXACTLY 0 bytes; RouterOS refused a
#    software queue outright with "non rate limit queues are useless on this
#    interface". Typical on only-hardware-queue ports and VLANs sharing a
#    trunk. If /queue tree print stats shows zero bytes during a speedtest,
#    this is that failure — switch back to the parent=global version above.
#    Trade-off in one line: interface-parented keeps FastTrack but is not
#    portable; parent=global is portable but costs you FastTrack.
#
# 4. Tunnelled IPv6 (6in4 / Hurricane Electric) is NOT covered. The outer
#    packets are IPv4 proto-41 addressed to the router itself (chain=input),
#    and the router's own encapsulated traffic leaves via chain=output — so
#    forward-chain rules miss it in both directions. Native IPv6 on the WAN
#    is shaped by the /ipv6 rules above; tunnelled IPv6 is not.
