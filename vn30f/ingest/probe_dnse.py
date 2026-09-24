"""Probe the DNSE LightSpeed KRX market-data broker.

Answers the question the data acquisition depended on: can a machine outside
Vietnam, with no DNSE account, subscribe to VN30 futures market data? The API docs
say some topics need no authentication. Docs are not a connection, so this connects
for real and reports what the broker actually grants.

Reports three things separately, because they fail for different reasons:
  1. TCP/TLS/WebSocket reach     -> geo-block or firewall shows up here
  2. MQTT CONNACK                -> anonymous auth policy shows up here
  3. SUBACK per topic            -> per-topic ACL shows up here (0x80 = refused)
  4. Messages received           -> whether the topic is actually populated

Run it during a trading session (VN 09:00-11:30 / 13:00-14:30) or step 4 is
meaningless: a granted subscription on a closed market is silent either way.

    python -m vn30f.ingest.probe_dnse --seconds 45
"""

from __future__ import annotations

import argparse
import json
import ssl
import time
import uuid
from collections import Counter
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

HOST = "datafeed-lts-krx.dnse.com.vn"
PORT = 443
WS_PATH = "/wss"

# Candidate topic patterns. DNSE moved to KRX-era topic names when KRX went live
# (2025-05-05) but the pre-KRX names may still be served, so both are probed. The
# broker's SUBACK tells us which exist and which are ACL'd; guessing is not needed.
CANDIDATE_TOPICS = [
    # --- KRX era ---
    "plaintext/quotes/krx/mdds/tick/v1/roundlot/symbol/+",
    "plaintext/quotes/krx/mdds/topprice/v1/roundlot/symbol/+",
    "plaintext/quotes/krx/mdds/v2/ohlc/derivative/1/+",
    "plaintext/quotes/krx/mdds/v2/ohlc/stock/1/+",
    "plaintext/quotes/krx/mdds/index/v1/+",
    "plaintext/quotes/krx/mdds/stockinfo/v1/+",
    # --- pre-KRX era ---
    "plaintext/quotes/stock/MDDS/TICK/v1/roundlot/symbol/+",
    "plaintext/quotes/stock/MDDS/TOP_PRICE/v1/roundlot/symbol/+",
    "plaintext/quotes/stock/MDDS/V2/OHLC/1/+",
    "plaintext/quotes/index/MI/+",
    # --- broad sweep: if the broker permits it, this reveals the real namespace ---
    "plaintext/#",
    "#",
]

# Front-month VN30 futures at the time of writing plus its ladder. Used for the
# targeted single-symbol probe when wildcards are refused.
PROBE_SYMBOLS = ["VN30F2608", "VN30F2609", "VN30F1M", "VN30"]


class Probe:
    def __init__(self, seconds: int, verbose: bool):
        self.seconds = seconds
        self.verbose = verbose
        self.connected = False
        self.connack: str | None = None
        self.sub_results: dict[int, tuple[str, str]] = {}  # mid -> (topic, grant)
        self.pending: dict[int, str] = {}                  # mid -> topic
        self.topic_counts: Counter[str] = Counter()
        self.first_payload: dict[str, str] = {}

    # -- callbacks ---------------------------------------------------------
    def on_connect(self, client, userdata, flags, reason_code, properties=None):
        self.connack = str(reason_code)
        self.connected = reason_code == 0 or getattr(reason_code, "is_failure", True) is False
        print(f"[CONNACK] {reason_code}  (session_present={flags.session_present if hasattr(flags, 'session_present') else flags})")
        if not self.connected:
            return
        for topic in CANDIDATE_TOPICS:
            result, mid = client.subscribe(topic, qos=1)
            self.pending[mid] = topic
        for sym in PROBE_SYMBOLS:
            for tmpl in (
                "plaintext/quotes/krx/mdds/tick/v1/roundlot/symbol/{}",
                "plaintext/quotes/krx/mdds/topprice/v1/roundlot/symbol/{}",
                "plaintext/quotes/krx/mdds/index/v1/{}",
            ):
                result, mid = client.subscribe(tmpl.format(sym), qos=1)
                self.pending[mid] = tmpl.format(sym)

    def on_subscribe(self, client, userdata, mid, reason_code_list, properties=None):
        topic = self.pending.pop(mid, f"<mid {mid}>")
        codes = ", ".join(str(rc) for rc in reason_code_list)
        self.sub_results[mid] = (topic, codes)

    def on_message(self, client, userdata, msg):
        self.topic_counts[msg.topic] += 1
        if msg.topic not in self.first_payload:
            try:
                body = msg.payload.decode("utf-8", errors="replace")
            except Exception:
                body = repr(msg.payload[:200])
            self.first_payload[msg.topic] = body[:600]
            if self.verbose:
                print(f"  [MSG] {msg.topic}\n        {body[:300]}")

    def on_disconnect(self, client, userdata, *args):
        print(f"[DISCONNECT] {args}")

    # -- driver ------------------------------------------------------------
    def run(self, username: str = "", password: str = "") -> int:
        client_id = f"dnse-probe-{uuid.uuid4().hex[:12]}"
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            transport="websockets",
            protocol=mqtt.MQTTv5,
        )
        client.ws_set_options(path=WS_PATH)
        client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        if username:
            client.username_pw_set(username, password)
        client.on_connect = self.on_connect
        client.on_subscribe = self.on_subscribe
        client.on_message = self.on_message
        client.on_disconnect = self.on_disconnect

        print(f"[DIAL] wss://{HOST}:{PORT}{WS_PATH}  client_id={client_id} "
              f"auth={'yes' if username else 'anonymous'}")
        t0 = time.time()
        try:
            client.connect(HOST, PORT, keepalive=60)
        except Exception as exc:
            print(f"[FAIL] transport/connect raised: {type(exc).__name__}: {exc}")
            print("       -> this is reach (TLS/WS/geo), not authentication.")
            return 2
        print(f"[DIAL] socket established in {time.time() - t0:.2f}s")

        client.loop_start()
        deadline = time.time() + self.seconds
        while time.time() < deadline:
            time.sleep(0.5)
        client.loop_stop()
        try:
            client.disconnect()
        except Exception:
            pass

        self.report()
        return 0 if self.topic_counts else (1 if self.connected else 2)

    def report(self) -> None:
        print("\n" + "=" * 72)
        print(f"PROBE REPORT  {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
        print("=" * 72)
        print(f"CONNACK        : {self.connack}")

        granted, refused = [], []
        for topic, codes in self.sub_results.values():
            (refused if "not authorized" in codes.lower() or "0x80" in codes
             or "Unspecified" in codes or "fail" in codes.lower() else granted).append((topic, codes))

        print(f"\nSUBSCRIPTIONS  : {len(granted)} granted, {len(refused)} refused, "
              f"{len(self.pending)} never answered")
        for topic, codes in sorted(granted):
            print(f"  GRANT  {topic}   [{codes}]")
        for topic, codes in sorted(refused):
            print(f"  DENY   {topic}   [{codes}]")
        for topic in sorted(self.pending.values()):
            print(f"  NOACK  {topic}")

        print(f"\nMESSAGES       : {sum(self.topic_counts.values())} across "
              f"{len(self.topic_counts)} topics")
        for topic, n in self.topic_counts.most_common(40):
            print(f"  {n:6d}  {topic}")
            sample = self.first_payload.get(topic, "")
            try:
                sample = json.dumps(json.loads(sample), ensure_ascii=False)[:300]
            except Exception:
                pass
            print(f"          {sample}")

        print("\nVERDICT:")
        if not self.connected:
            print("  NO-GO on DNSE. Broker refused the connection itself.")
        elif not self.topic_counts:
            print("  Connected but silent. Either every topic is ACL'd, the topic")
            print("  names are wrong, or the market is closed. Check the SUBACK list")
            print("  above and re-run inside 09:00-11:30 / 13:00-14:30 VN time.")
        else:
            print("  GO. Live data is reaching this machine without a DNSE account.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=int, default=45, help="listen window")
    ap.add_argument("--username", default="", help="DNSE investorId, if you have one")
    ap.add_argument("--password", default="", help="JWT, if you have one")
    ap.add_argument("--quiet", action="store_true", help="suppress per-message echo")
    args = ap.parse_args()
    return Probe(args.seconds, verbose=not args.quiet).run(args.username, args.password)


if __name__ == "__main__":
    raise SystemExit(main())
