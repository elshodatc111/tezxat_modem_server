"""GSM modem drayveri (AT buyruqlar, PDU rejimi) va test uchun MockTransport."""
from __future__ import annotations

import re
import threading
import time
from collections import deque

from . import pdu as pdumod


class ModemError(Exception):
    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal   # True: port yo'qoldi / modem javob bermayapti -> qayta ulanish kerak


# ----------------------------------------------------------------------------- transport
class SerialTransport:
    def __init__(self, port: str, baudrate: int = 115200):
        import serial  # pyserial

        self._serial_exc = (serial.SerialException, OSError)
        try:
            self.ser = serial.Serial(port, baudrate, timeout=0.1, write_timeout=5)
        except self._serial_exc as e:
            raise ModemError(f"Portni ochib bo'lmadi ({port}): {e}", fatal=True)

    def write(self, data: bytes) -> None:
        try:
            self.ser.write(data)
            self.ser.flush()
        except self._serial_exc as e:
            raise ModemError(f"Portga yozib bo'lmadi: {e}", fatal=True)

    def readline(self, timeout: float) -> bytes:
        end = time.monotonic() + timeout
        buf = bytearray()
        try:
            while time.monotonic() < end:
                b = self.ser.read(1)
                if not b:
                    if buf.strip() == b">":      # SMS kiritish taklifi: "> " (\n siz)
                        return bytes(buf)
                    continue
                buf += b
                if b == b"\n":
                    return bytes(buf)
        except self._serial_exc as e:
            raise ModemError(f"Portdan o'qib bo'lmadi: {e}", fatal=True)
        return bytes(buf)

    def reset_input(self) -> None:
        try:
            self.ser.reset_input_buffer()
        except self._serial_exc:
            pass

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:
            pass


class MockTransport:
    """Haqiqiy modemsiz test: AT buyruqlarga javob beradi, SMS larni xotirada saqlaydi, status report yuboradi."""

    def __init__(self, name: str = "mock", report: bool = True, imei: str = "860000000000001"):
        self.name = name
        self.report = report
        self.imei = imei
        self.out: deque[bytes] = deque()
        self._buf = b""
        self._await_pdu = False
        self._cmgs_len = 0
        self._mr = 0
        self.sent: list[tuple[str, str]] = []     # (telefon, matn) — qismlar birlashtirilgan holda
        self._parts: dict[int, dict[int, str]] = {}
        self.fail_next = 0            # keyingi N ta CMGS xato bilan tugaydi
        self.fail_text = "+CMS ERROR: 500"
        self.balance_text = "Balans: 12 500 som. Bonus: 0"      # *100# javobi (testlarda o'zgartiriladi)
        self.closed = False
        self.lock = threading.Lock()

    # --- yordamchilar
    def _put(self, *lines: str) -> None:
        for ln in lines:
            self.out.append((ln + "\r\n").encode())

    def write(self, data: bytes) -> None:
        if self.closed:
            raise ModemError("Mock port yopilgan", fatal=True)
        with self.lock:
            self._buf += data
            if self._await_pdu:
                if b"\x1a" in self._buf:
                    pdu_hex, _, rest = self._buf.partition(b"\x1a")
                    self._buf = rest
                    self._await_pdu = False
                    self._on_pdu(pdu_hex.decode().strip())
                return
            while b"\r" in self._buf:
                line, _, self._buf = self._buf.partition(b"\r")
                cmd = line.decode(errors="ignore").strip()
                if cmd:
                    self._on_cmd(cmd)
                    if self._await_pdu:
                        break

    def readline(self, timeout: float) -> bytes:
        with self.lock:
            if self.out:
                return self.out.popleft()
        time.sleep(min(timeout, 0.01))
        return b""

    def reset_input(self) -> None:
        with self.lock:
            self.out.clear()

    def close(self) -> None:
        self.closed = True

    # --- modem mantig'i
    def _on_cmd(self, cmd: str) -> None:
        u = cmd.upper()
        if u in ("AT", "ATE0", "ATZ") or u.startswith(("AT+CMEE", "AT+CMGF", "AT+CSCS", "AT+CNMI", "AT+CMGD", "AT+COPS=")):
            self._put("OK")
        elif u == "AT+CPIN?":
            self._put("+CPIN: READY", "OK")
        elif u == "AT+CGMM":
            self._put("MOCK-MODEM-E173", "OK")
        elif u == "AT+CGSN":
            self._put(self.imei, "OK")
        elif u == "AT+CSQ":
            self._put("+CSQ: 21,99", "OK")
        elif u == "AT+COPS?":
            self._put('+COPS: 0,0,"MOCK-OP",2', "OK")
        elif u.startswith("AT+CMGS="):
            self._cmgs_len = int(u.split("=")[1])
            self._await_pdu = True
            self.out.append(b"> ")
        elif u.startswith("AT+CUSD=2"):
            self._put("OK")
        elif u.startswith("AT+CUSD=1"):
            m = re.search(r'"(.*?)"', cmd)
            code = m.group(1) if m else "?"
            self._put("OK")
            if "100" in code:
                self._put(f'+CUSD: 0,"{self.balance_text}",15')
            elif "107" in code or "paket" in code.lower():
                self._put('+CUSD: 0,"SMS paket: 480 ta qoldi\nMuddati: 30.11.2026",15')
            else:
                self._put(f'+CUSD: 0,"Mock USSD javobi: {code}",15')
        else:
            self._put("ERROR")

    def _on_pdu(self, pdu_hex: str) -> None:
        if self.fail_next > 0:
            self.fail_next -= 1
            self._put(self.fail_text)
            return
        if len(pdu_hex) // 2 - 1 != self._cmgs_len:
            self._put("+CMS ERROR: 304")
            return
        self._mr = (self._mr + 1) % 256
        mr = self._mr
        text = pdumod.decode_deliver_text(pdu_hex)
        # qabul qiluvchi raqam
        i = 2 + int(pdu_hex[0:2], 16) * 2 + 4
        da_len = int(pdu_hex[i:i + 2], 16)
        digits = pdumod._unswap(pdu_hex[i + 4:i + 4 + ((da_len + 1) // 2) * 2])
        first = int(pdu_hex[2:4], 16)
        if first & 0x40:   # uzun SMS: qismlarni yig'ish
            ud_start = i + 4 + ((da_len + 1) // 2) * 2 + 6
            udl_bytes = bytes.fromhex(pdu_hex[ud_start:])
            ref, total, seq = udl_bytes[3], udl_bytes[4], udl_bytes[5]
            bucket = self._parts.setdefault(ref, {})
            bucket[seq] = text
            if len(bucket) == total:
                self.sent.append((digits, "".join(bucket[k] for k in sorted(bucket))))
                self._parts.pop(ref, None)
        else:
            self.sent.append((digits, text))
        self._put(f"+CMGS: {mr}", "OK")
        if self.report and first & 0x20:
            addr = f"{len(digits):02X}91{pdumod._swap_digits(digits)}"
            sr = f"0006{mr:02X}{addr}6210201215320062102012153300" + "00"
            self._put(f"+CDS: {len(sr) // 2}", sr)


# ----------------------------------------------------------------------------- modem
_URC_PREFIXES = ("^", "+CMTI", "+CREG", "+CGREG", "+CEREG", "RING", "NO CARRIER", "+CLIP", "+CMT:", "+CBM", "*PSUTTZ", "+CTZV", "DST:", "SMS DONE", "PB DONE", "+SIM", "+CIEV")
_CUSD_FULL = re.compile(r'^\+CUSD:\s*(\d+)(?:\s*,\s*"(.*)"\s*(?:,\s*(\d+))?)?\s*$', re.S)


class ATModem:
    def __init__(self, transport):
        self.t = transport
        self.reports: list[pdumod.StatusReport] = []
        self.report_indexes: list[int] = []
        self.cusd_results: list[str] = []
        self._cusd_acc: str | None = None
        self._expect_pdu = False
        self.model: str | None = None
        self.imei: str | None = None

    # --- past daraja
    @staticmethod
    def _decode(raw: bytes) -> str:
        return raw.decode("latin-1", errors="ignore").strip()

    def _read(self, timeout: float) -> str | None:
        raw = self.t.readline(timeout)
        if not raw:
            return None
        line = self._decode(raw)
        return line if line != "" else ""

    def _consume_urc(self, line: str) -> bool:
        """URC (o'z-o'zidan kelgan xabar)ni qayta ishlaydi; qayta ishlangan bo'lsa True."""
        if self._expect_pdu:
            self._expect_pdu = False
            if re.fullmatch(r"[0-9A-Fa-f]{16,}", line):
                try:
                    self.reports.append(pdumod.decode_status_report(line))
                except Exception:
                    pass
                return True
        if self._cusd_acc is not None:
            self._cusd_acc += "\n" + line
            if _CUSD_FULL.match(self._cusd_acc):
                self.cusd_results.append(self._cusd_acc)
                self._cusd_acc = None
            return True
        if line.startswith("+CUSD:"):
            if _CUSD_FULL.match(line):
                self.cusd_results.append(line)
            else:
                self._cusd_acc = line
            return True
        if line.startswith("+CDSI:"):
            m = re.search(r",\s*(\d+)\s*$", line)
            if m:
                self.report_indexes.append(int(m.group(1)))
            return True
        if line.startswith("+CDS:"):
            self._expect_pdu = True
            return True
        return line.startswith(_URC_PREFIXES)

    def cmd(self, command: str, timeout: float = 5.0) -> list[str]:
        """Buyruq yuboradi, OK gacha javob qatorlarini qaytaradi. ERROR bo'lsa ModemError."""
        self.t.write((command + "\r").encode())
        lines: list[str] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self._read(min(1.0, max(0.05, deadline - time.monotonic())))
            if not line:
                continue
            if line == command:         # echo
                continue
            if line == "OK":
                return lines
            if line == "ERROR" or line.startswith(("+CME ERROR", "+CMS ERROR")):
                raise ModemError(f"{command} -> {line}")
            if self._consume_urc(line):
                continue
            lines.append(line)
        raise ModemError(f"Javob kelmadi (timeout): {command}", fatal=True)

    def poll(self, max_lines: int = 40) -> None:
        """Kutilmagan xabarlarni (status report, USSD) o'qib oladi. Bloklanmaydi."""
        for _ in range(max_lines):
            line = self._read(0.02)
            if not line:
                break
            self._consume_urc(line)
        # +CDSI orqali xotiraga saqlangan status reportlar
        while self.report_indexes:
            idx = self.report_indexes.pop(0)
            try:
                lines = self.cmd(f"AT+CMGR={idx}", timeout=5)
                for ln in lines:
                    if re.fullmatch(r"[0-9A-Fa-f]{16,}", ln):
                        try:
                            self.reports.append(pdumod.decode_status_report(ln))
                        except Exception:
                            pass
                self.cmd(f"AT+CMGD={idx}", timeout=5)
            except ModemError as e:
                if e.fatal:
                    raise

    def take_reports(self) -> list[pdumod.StatusReport]:
        out, self.reports = self.reports, []
        return out

    # --- yuqori daraja
    def init(self, pin: str | None = None) -> None:
        last: Exception | None = None
        for _ in range(4):
            try:
                self.t.reset_input()
                self.cmd("AT", timeout=3)
                last = None
                break
            except ModemError as e:
                last = e
                time.sleep(0.5)
        if last:
            raise ModemError(f"Modem javob bermayapti: {last}", fatal=True)

        self.cmd("ATE0", timeout=3)
        for c in ("AT+CMEE=1",):
            try:
                self.cmd(c, 3)
            except ModemError:
                pass

        state = self.sim_state()
        if state == "SIM PIN":
            if not pin:
                raise ModemError("SIM karta PIN so'raydi, lekin PIN kiritilmagan")
            self.cmd(f'AT+CPIN="{pin}"', timeout=10)
            time.sleep(2)
        elif state not in ("READY", None):
            raise ModemError(f"SIM holati: {state}")

        self.cmd("AT+CMGF=0", timeout=3)               # PDU rejimi
        try:
            self.cmd('AT+CSCS="GSM"', 3)
        except ModemError:
            pass
        for cnmi in ("AT+CNMI=2,0,0,2,0", "AT+CNMI=2,0,0,1,0"):   # yetkazish hisobotlari (status report)
            try:
                self.cmd(cnmi, 3)
                break
            except ModemError:
                continue

        try:
            lines = self.cmd("AT+CGMM", 3)
            self.model = lines[0] if lines else None
        except ModemError:
            pass
        try:
            lines = self.cmd("AT+CGSN", 3)
            self.imei = next((ln for ln in lines if re.fullmatch(r"\d{10,20}", ln)), None)
        except ModemError:
            pass

    def sim_state(self) -> str | None:
        try:
            lines = self.cmd("AT+CPIN?", 5)
        except ModemError as e:
            msg = str(e)
            if "SIM not inserted" in msg or "10" in msg.split("ERROR:")[-1]:
                return "NO SIM"
            raise
        for ln in lines:
            if ln.startswith("+CPIN:"):
                return ln.split(":", 1)[1].strip()
        return None

    def signal(self) -> int | None:
        for ln in self.cmd("AT+CSQ", 3):
            m = re.match(r"\+CSQ:\s*(\d+),", ln)
            if m:
                v = int(m.group(1))
                return None if v == 99 else v
        return None

    def operator(self) -> str | None:
        for ln in self.cmd("AT+COPS?", 5):
            m = re.match(r'\+COPS:\s*\d+(?:,\d+,"([^"]*)")?', ln)
            if m:
                return m.group(1) or None
        return None

    def send_sms(self, phone: str, text: str, request_report: bool = True) -> list[int]:
        """SMS yuboradi. Har bir qism uchun modem bergan MR (message reference) ro'yxatini qaytaradi."""
        parts = pdumod.encode_submit(phone, text, request_report=request_report)
        mrs: list[int] = []
        for p in parts:
            self.t.write(f"AT+CMGS={p.tpdu_len}\r".encode())
            deadline = time.monotonic() + 10
            prompt = False
            while time.monotonic() < deadline:
                line = self._read(1.0)
                if not line:
                    continue
                if line.startswith(">"):
                    prompt = True
                    break
                if line == "ERROR" or line.startswith(("+CME ERROR", "+CMS ERROR")):
                    raise ModemError(f"AT+CMGS -> {line}")
                self._consume_urc(line)
            if not prompt:
                raise ModemError("Modem SMS kiritishga ruxsat bermadi (prompt yo'q)", fatal=True)

            self.t.write(p.pdu.encode() + b"\x1a")
            deadline = time.monotonic() + 60
            mr: int | None = None
            while time.monotonic() < deadline:
                line = self._read(1.0)
                if not line:
                    continue
                if line.startswith("+CMGS:"):
                    try:
                        mr = int(line.split(":")[1].strip())
                    except ValueError:
                        mr = 0
                    continue
                if line == "OK":
                    break
                if line == "ERROR" or line.startswith(("+CME ERROR", "+CMS ERROR")):
                    raise ModemError(f"SMS yuborilmadi: {line}")
                self._consume_urc(line)
            else:
                raise ModemError("SMS yuborish vaqti tugadi (60s)", fatal=True)
            mrs.append(mr if mr is not None else 0)
        return mrs

    def ussd(self, code: str, timeout: float = 45.0) -> str:
        self.cusd_results.clear()
        self._cusd_acc = None
        self.cmd(f'AT+CUSD=1,"{code}",15', timeout=15)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.cusd_results:
                break
            line = self._read(0.5)
            if line:
                self._consume_urc(line)
        if not self.cusd_results:
            raise ModemError("USSD javob kelmadi")
        m = _CUSD_FULL.match(self.cusd_results.pop(0))
        status = int(m.group(1)) if m else 0
        text = (m.group(2) or "") if m else ""
        dcs = int(m.group(3)) if (m and m.group(3)) else 15
        if status == 1:      # operator davomini kutmoqda — sessiyani yopamiz
            try:
                self.cmd("AT+CUSD=2", 5)
            except ModemError:
                pass
        if status in (4,):
            raise ModemError("USSD qo'llab-quvvatlanmaydi")
        return decode_ussd(text, dcs)

    def cleanup_memory(self) -> None:
        """SIM xotirasida to'planib qolgan kiruvchi SMS/hisobotlarni tozalash."""
        try:
            self.cmd("AT+CMGD=1,4", 10)
        except ModemError as e:
            if e.fatal:
                raise

    def close(self) -> None:
        self.t.close()


def decode_ussd(text: str, dcs: int = 15) -> str:
    """USSD javobini o'qiladigan matnga aylantiradi (UCS2 hex yoki 7-bit hex bo'lishi mumkin)."""
    t = text.strip()
    if re.fullmatch(r"(?:[0-9A-Fa-f]{2}){8,}", t):
        raw = bytes.fromhex(t)
        if dcs in (8, 72) or (len(raw) % 2 == 0 and dcs not in (0, 15, 17) and raw[:1] in (b"\x00", b"\x04")):
            try:
                return raw.decode("utf-16-be")
            except UnicodeDecodeError:
                pass
        if dcs in (0, 15, 17, 68):
            try:
                count = len(raw) * 8 // 7
                return pdumod.gsm7_decode(pdumod.unpack_septets(raw, count))
            except Exception:
                pass
    return t
