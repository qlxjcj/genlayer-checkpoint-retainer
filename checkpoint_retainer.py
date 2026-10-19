# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
import json
from genlayer import *

MAX_CHECKPOINTS = 6
MAX_ID = 32
MAX_LABEL = 80
MAX_MARKER = 80
MAX_URL = 220
MIN_GRACE = 1

OPEN = "OPEN"
CLOSED = "CLOSED"
REFUNDED = "REFUNDED"

PENDING = "PENDING"
PAID = "PAID"
MISSED = "MISSED"

SPACE = " \t\r\n\f\v"


class CheckpointRetainer(gl.Contract):
    """A time-indexed retainer. The payer escrows a total, splits it into
    checkpoints with due dates, and each checkpoint pays only when two
    independent sources both report it complete. Nobody files anything: the
    judged set is the two sources, fixed at creation. A checkpoint that misses
    never blocks a later one, and the unearned remainder goes home at finalize.
    See README."""

    vaults: TreeMap[str, str]
    checks: TreeMap[str, str]
    vault_count: u256

    def __init__(self):
        self.vault_count = 0

    # ---------------- helpers ----------------

    def _addr_hex(self, a) -> str:
        if isinstance(a, str):
            return a
        h = getattr(a, "as_hex", None)
        if isinstance(h, str):
            return h
        if isinstance(a, (bytes, bytearray)):
            return "0x" + bytes(a).hex()
        return str(a)

    def _is_valid_address(self, addr) -> bool:
        if not isinstance(addr, str) or not addr.startswith("0x") or len(addr) != 42:
            return False
        try:
            int(addr[2:], 16)
        except ValueError:
            return False
        return True

    def _iso_to_epoch(self, s: str) -> int:
        s = s.strip()
        if len(s) < 19:
            return 0
        offset = 0
        tail = s[19:]
        if tail.endswith("Z"):
            tail = tail[:-1]
        if tail and (tail[0] == "+" or tail[0] == "-"):
            sign = 1 if tail[0] == "+" else -1
            rest = tail[1:5]
            hh = int(rest[0:2]) if rest[0:2].isdigit() else 0
            mm = int(rest[2:4]) if rest[2:4].isdigit() else 0
            offset = sign * (hh * 3600 + mm * 60)
        days = 0
        y = int(s[0:4])
        mo = int(s[5:7])
        d = int(s[8:10])
        for yr in range(1970, y):
            days += 366 if (yr % 4 == 0 and (yr % 100 != 0 or yr % 400 == 0)) else 365
        cum = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
        days += cum[mo - 1]
        if mo > 2 and (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)):
            days += 1
        days += d - 1
        hh = int(s[11:13]) if s[11:13].isdigit() else 0
        mi = int(s[14:16]) if s[14:16].isdigit() else 0
        ss = int(s[17:19]) if s[17:19].isdigit() else 0
        return days * 86400 + hh * 3600 + mi * 60 + ss - offset

    def _now(self) -> int:
        raw = getattr(gl, "message_raw", None)
        d = None
        if raw is not None:
            try:
                d = raw["datetime"]
            except Exception:
                d = None
        if d is None:
            d = getattr(gl.message, "datetime", None)
        if d is None:
            return 0
        return self._iso_to_epoch(d if isinstance(d, str) else str(d))

    def _squash(self, text: str) -> str:
        """lowercase, every blank removed: survives line wrap and stray padding."""
        out = []
        for ch in str(text).lower():
            if ch in SPACE:
                continue
            out.append(ch)
        return "".join(out)

    def _host(self, url: str) -> str:
        """scheme://host/path -> host, lowercased, no port or userinfo."""
        u = str(url).strip().lower()
        i = u.find("://")
        if i >= 0:
            u = u[i + 3:]
        for ch in "/?#":
            j = u.find(ch)
            if j >= 0:
                u = u[:j]
        k = u.rfind("@")
        if k >= 0:
            u = u[k + 1:]
        c = u.rfind(":")
        if c >= 0:
            u = u[:c]
        return u

    def _decode_body(self, content) -> str:
        body = getattr(content, "body", None)
        if body is None:
            return str(content)
        return body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)

    def _transfer(self, recipient: Address, amount: int):
        if amount > 0:
            gl.get_contract_at(recipient).emit_transfer(value=u256(amount))

    def _ckey(self, vault_id: str, index: int) -> str:
        return str(vault_id) + "|" + str(int(index))

    def _load(self, vault_id: str) -> dict:
        raw = self.vaults.get(str(vault_id), "")
        if not raw:
            raise gl.vm.UserError("Vault not found")
        return json.loads(raw)

    def _save(self, v: dict):
        self.vaults[str(v["vault_id"])] = json.dumps(v)

    def _load_ck(self, vault_id: str, index: int) -> dict:
        raw = self.checks.get(self._ckey(vault_id, index), "")
        if not raw:
            raise gl.vm.UserError("No such checkpoint")
        return json.loads(raw)

    def _save_ck(self, vault_id: str, index: int, ck: dict):
        self.checks[self._ckey(vault_id, index)] = json.dumps(ck)

    # ---------------- evidence ----------------

    def _examine(self, sources: list, marker: str) -> dict:
        """Fetch both sources; the verdict is computed in code, in the block."""

        def check() -> dict:
            bodies = []
            for u in sources:
                try:
                    bodies.append(self._decode_body(gl.nondet.web.render(str(u))))
                except Exception:
                    bodies.append("")
            if not bodies[0] or not bodies[1]:
                return {"retrieved": False, "ok": False, "spans": ["", ""]}
            spans = []
            for b in bodies:
                task = (
                    "A retainer pays one checkpoint when a report says it was done.\n"
                    "Return the ONE sentence that contains MARKER and reports it as done\n"
                    "(shipped / released / delivered / complete). Copy it exactly. If no\n"
                    "such sentence exists, return an empty string.\n\n"
                    "MARKER: " + marker + "\nREPORT:\n" + b[:900] +
                    '\n\nJSON only: {"span": "<sentence or empty>"}'
                )
                r = gl.nondet.exec_prompt(task)
                if isinstance(r, str):
                    r = json.loads(r.replace("```json", "").replace("```", ""))
                spans.append(str(r.get("span", "")) if isinstance(r, dict) else "")
            ok = bool(spans[0]) and bool(spans[1])
            want = self._squash(marker)
            for i in (0, 1):
                if not ok:
                    break
                if want not in self._squash(spans[i]):
                    ok = False
                elif self._squash(spans[i]) not in self._squash(bodies[i]):
                    ok = False
            return {"retrieved": True, "ok": ok, "spans": [spans[0], spans[1]]}

        principle = ("Equivalent only if retrieved and ok match exactly; the span is an "
                     "audit trail and may differ in wording, but empty stays empty.")
        return gl.eq_principle.prompt_comparative(check, principle)

    # ---------------- lifecycle ----------------

    @gl.public.write.payable
    def open_vault(self, recipient: str, sources: list, checkpoints: list, grace: int) -> str:
        """Open a vault: the payer names the two sources and the schedule."""
        payer = self._addr_hex(gl.message.sender_address)
        total = int(gl.message.value)
        if not self._is_valid_address(recipient):
            raise gl.vm.UserError("Recipient must be 0x + 40 hex")
        if self._addr_hex(recipient).lower() == payer.lower():
            raise gl.vm.UserError("Recipient must differ from payer")
        if not isinstance(sources, list) or len(sources) != 2:
            raise gl.vm.UserError("Exactly two sources are required")
        urls = []
        for u in sources:
            if not isinstance(u, str):
                raise gl.vm.UserError("Sources must be http(s)")
            u = u.strip()
            if not (u.startswith("http://") or u.startswith("https://")):
                raise gl.vm.UserError("Sources must be http(s)")
            if len(u) > MAX_URL:
                raise gl.vm.UserError("Source url too long")
            urls.append(u)
        h0 = self._host(urls[0])
        h1 = self._host(urls[1])
        if not h0 or not h1 or h0 == h1:
            raise gl.vm.UserError("Sources must be from different hosts")
        if int(grace) < MIN_GRACE:
            raise gl.vm.UserError("Grace must be at least " + str(MIN_GRACE))
        if not isinstance(checkpoints, list) or not checkpoints or len(checkpoints) > MAX_CHECKPOINTS:
            raise gl.vm.UserError("Checkpoints must be 1.." + str(MAX_CHECKPOINTS))
        if total <= 0:
            raise gl.vm.UserError("Escrow must be > 0")

        chips = []
        sum_amt = 0
        prev = 0
        for k in checkpoints:
            if not isinstance(k, dict):
                raise gl.vm.UserError("Each checkpoint must be an object")
            label = str(k.get("label", "")).strip()
            marker = str(k.get("marker", "")).strip()
            if not label or len(label) > MAX_LABEL:
                raise gl.vm.UserError("Checkpoint label required")
            if not marker or len(marker) > MAX_MARKER:
                raise gl.vm.UserError("Checkpoint marker required")
            try:
                amt = int(str(k.get("amount", "0")))
                due = int(str(k.get("due_at", "0")))
            except Exception:
                raise gl.vm.UserError("Checkpoint amount and due_at must be integers")
            if amt <= 0:
                raise gl.vm.UserError("Checkpoint amount must be positive")
            if due <= prev:
                raise gl.vm.UserError("Checkpoint due dates must increase")
            prev = due
            sum_amt += amt
            chips.append({"index": len(chips), "label": label, "amount": str(amt),
                          "due_at": str(due), "marker": marker, "status": PENDING,
                          "span": "", "reason": "", "attempts": "0"})
        if sum_amt != total:
            raise gl.vm.UserError("Checkpoint amounts must sum to the escrow")

        self.vault_count += 1
        vid = str(int(self.vault_count))
        self.vaults[vid] = json.dumps({
            "vault_id": vid, "payer": payer, "recipient": self._addr_hex(recipient),
            "sources": urls, "grace": str(int(grace)), "total": str(total),
            "paid_total": "0", "refunded": "0", "status": OPEN,
            "count": str(len(chips)), "last_due": str(prev),
            "first_due": str(int(chips[0]["due_at"])), "opened_at": str(self._now()),
        })
        for ck in chips:
            self._save_ck(vid, ck["index"], ck)
        return vid

    @gl.public.write
    def release(self, vault_id: str, index: int) -> str:
        """Pay one checkpoint, once it is due, if both sources report it done."""
        v = self._load(vault_id)
        if str(v["status"]) != OPEN:
            raise gl.vm.UserError("Vault is " + str(v["status"]))
        ck = self._load_ck(vault_id, index)
        if str(ck["status"]) != PENDING:
            raise gl.vm.UserError("Checkpoint is " + str(ck["status"]))
        if self._now() < int(ck["due_at"]):
            raise gl.vm.UserError("Checkpoint is not due yet")

        res = self._examine(v["sources"], str(ck["marker"]))
        if not (isinstance(res, dict) and isinstance(res.get("ok"), bool)):
            raise gl.vm.UserError("Invalid examination result")
        spans = res.get("spans")
        if not isinstance(spans, list) or len(spans) != 2:
            spans = ["", ""]

        ck["attempts"] = str(int(ck["attempts"]) + 1)
        if res.get("ok") is True:
            ck["status"] = PAID
            ck["span"] = str(spans[0])
            ck["reason"] = "both sources report the checkpoint complete"
            self._save_ck(vault_id, index, ck)
            amount = int(ck["amount"])
            v["paid_total"] = str(int(v["paid_total"]) + amount)
            self._save(v)
            self._transfer(Address(v["recipient"]), amount)
            return PAID

        # Not proven yet. The checkpoint stays open, because evidence often
        # arrives late; finalize is what closes it for good.
        ck["reason"] = ("a source was unreachable" if not res.get("retrieved")
                        else "not reported complete by both sources")
        self._save_ck(vault_id, index, ck)
        return "NOT_YET"

    @gl.public.write
    def finalize(self, vault_id: str) -> str:
        """Close after the last deadline: whatever was never earned goes home."""
        v = self._load(vault_id)
        if str(v["status"]) != OPEN:
            raise gl.vm.UserError("Vault is " + str(v["status"]))
        if self._now() < int(v["last_due"]) + int(v["grace"]):
            raise gl.vm.UserError("Grace period has not ended")
        for i in range(int(v["count"])):
            ck = self._load_ck(vault_id, i)
            if str(ck["status"]) == PENDING:
                ck["status"] = MISSED
                ck["reason"] = "deadline passed without proof from both sources"
                self._save_ck(vault_id, i, ck)
        back = int(v["total"]) - int(v["paid_total"])
        v["refunded"] = str(back)
        v["status"] = CLOSED
        self._save(v)
        self._transfer(Address(v["payer"]), back)
        return CLOSED

    @gl.public.write
    def cancel(self, vault_id: str) -> str:
        """The payer may take the whole thing back before the first deadline."""
        v = self._load(vault_id)
        if str(v["status"]) != OPEN:
            raise gl.vm.UserError("Vault is " + str(v["status"]))
        if self._addr_hex(gl.message.sender_address).lower() != str(v["payer"]).lower():
            raise gl.vm.UserError("Only the payer cancels")
        if int(v["paid_total"]) != 0:
            raise gl.vm.UserError("Already paid out")
        if self._now() >= int(v["first_due"]):
            raise gl.vm.UserError("The first checkpoint is already due")
        v["refunded"] = v["total"]
        v["status"] = REFUNDED
        self._save(v)
        self._transfer(Address(v["payer"]), int(v["total"]))
        return REFUNDED

    # ---------------- views ----------------

    @gl.public.view
    def get_vault(self, vault_id: str) -> str:
        v = self._load(vault_id)
        out = {k: v[k] for k in ("vault_id", "payer", "recipient", "sources", "grace",
                                 "total", "paid_total", "refunded", "status",
                                 "first_due", "last_due", "opened_at")}
        out["checkpoints"] = [self._load_ck(vault_id, i) for i in range(int(v["count"]))]
        return json.dumps(out)

    @gl.public.view
    def get_checkpoint(self, vault_id: str, index: int) -> str:
        return json.dumps(self._load_ck(vault_id, index))

    @gl.public.view
    def get_vault_count(self) -> int:
        return int(self.vault_count)
