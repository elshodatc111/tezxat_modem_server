# SMS Modem Server (Python)

Local Ubuntu serverda ishlaydi. Vazifasi:

- **Hostingdagi Laravel navbatini** har 2–3 soniyada tekshiradi (yoki long-poll), SMS larni oladi;
- **bir nechta USB modem** orqali yuboradi: modem bo'shashi bilan keyingi SMS ni o'zi oladi → *biri band bo'lsa, ikkinchisi avtomatik ishlaydi; hammasi band bo'lsa SMS lar navbatda kutadi*;
- natijani (`sent` / `delivered` / `failed`) hostingga qaytaradi (xato bo'lsa hostingda pul foydalanuvchiga qaytariladi);
- **web panel** (port 8088): modemlar holati, signal, operator, **SIM balans va SMS paket (USSD)**, test SMS, navbat, sozlamalar (hosting URL/token — hosting ko'chirilsa shu yerda o'zgartirasiz), jurnal.

Ikki jarayon (ikkalasi ham bitta MySQL bazadan foydalanadi):

| Jarayon | Buyruq | Vazifasi |
|---|---|---|
| `sms-modem-worker` | `python -m app.worker` | Modem portlarining yagona egasi + hosting bilan sinxronizatsiya |
| `sms-modem-web` | `uvicorn app.web:app` | Boshqaruv paneli (modemga to'g'ridan-to'g'ri tegmaydi, buyruqlarni bazaga yozadi) |

Hamma kod — Python (FastAPI + SQLAlchemy + pyserial + httpx).

## 1. Modemni tekshirish (Ubuntu, SSH orqali)

```bash
lsusb                                  # modem ko'rinishi kerak (Huawei, ZTE, ...)
ls /dev/ttyUSB* /dev/serial/by-id/     # portlar
sudo dmesg | tail -30                  # "ttyUSB0..2" paydo bo'lganini tekshiring
sudo apt install -y minicom && sudo minicom -D /dev/ttyUSB0 -b 115200
#   AT  -> OK ;  AT+CPIN?  -> +CPIN: READY ;  AT+CSQ ;  AT+COPS?
```

Modem faqat "CD-ROM" ko'rinsa: `sudo apt install usb-modeswitch` va qayta ulang. Bir nechta port bo'lsa (ttyUSB0/1/2) AT ga **OK** deydigan (odatda `ttyUSB0` yoki `ttyUSB2`) portni tanlang. Barqaror nom uchun `/dev/serial/by-id/...` dan foydalaning.

## 2. MySQL

```sql
CREATE DATABASE sms_modem CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'sms_modem'@'localhost' IDENTIFIED BY 'KUCHLI_PAROL';
GRANT ALL ON sms_modem.* TO 'sms_modem'@'localhost';
```

(Jadvallar birinchi ishga tushishda avtomatik yaratiladi.)

## 3. O'rnatish

```bash
git clone <sizning-repo> sms-modem-server && cd sms-modem-server
sudo bash deploy/setup_ubuntu.sh       # /opt/sms-modem-server, venv, systemd
sudo nano /opt/sms-modem-server/.env   # DATABASE_URL, ADMIN_PASSWORD
sudo systemctl enable --now sms-modem-worker sms-modem-web
journalctl -u sms-modem-worker -f      # loglar
```

Panel: `http://SERVER_IP:8088` (login/parol — `.env` dagi `ADMIN_USER` / `ADMIN_PASSWORD`).

1. **Modemlar → Yangi modem**: nom, port, SIM balans/paket USSD kodlari (operatoringizdan aniqlang), kunlik limit.
2. **Sozlamalar**: `Hosting URL` va `Gateway token` (hosting admin panelidan: *Gateway → Token yaratish*) → «Ulanishni tekshirish».
3. Modem sahifasida «Test SMS» bilan sinang.

> Server ichki tarmoqda bo'lsa ham ishlaydi: u hostingga **o'zi** ulanadi (statik IP, port ochish, tunnel shart emas). Panelni tashqaridan ochmang — faqat LAN yoki VPN/Cloudflare Access orqali.

## 4. Modemsiz demo (Windows/Linux, MySQL kerak emas)

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
./run_local_demo.sh                                     # Windows: .env da CREATE_DEMO_MODEM=1, ikki terminalda:
                                                        #   python -m app.worker      va     python -m uvicorn app.web:app --port 8088
```

`mock://1`, `mock://2` — ikkita soxta modem (SMS yuboradi, USSD ga javob beradi, «yetkazildi» hisobotini qaytaradi).

## 5. Sozlamalar tavsifi

| Sozlama | Ma'nosi |
|---|---|
| Tekshirish oralig'i | Hostingdan navbat so'rash chastotasi (2–3 s). Navbat bo'lsa kutmasdan davom etadi |
| Long-poll | `>0` bo'lsa hosting shuncha soniya javobni ushlab turadi (kechikish ~0). Hosting PHP vaqt limitidan kichik bo'lsin |
| Mahalliy navbat sig'imi | To'lsa hostingdan yangi olinmaydi (SMS lar hostingda «navbatda» turadi) |
| Modem: kunlik limit / pauza | SIM bloklanmasligi uchun himoya |
| Maks. urinishlar | Modem xatosida boshqa (yoki shu) modem bilan qayta urinadi; keyin `failed` + pul qaytariladi |

## 6. Qanday ishlaydi (muhim nuqtalar)

- **Bitta SMS = bitta modem.** Modem oqimi `SELECT … FOR UPDATE SKIP LOCKED` bilan keyingi `pending` SMS ni oladi.
- **Dublikatsiyadan himoya:** hosting `processing` holatiga o'tkazadi; `remote_id` mahalliy bazada `UNIQUE`.
- **Hosting uzilsa:** SMS yuborish davom etadi, natijalar saqlanib turadi va aloqa tiklangach qayta yuboriladi (`reported_status`).
- **Modem uzilsa:** port qayta ochishga har 10 s urinadi; `sending` da qolgan SMS qayta navbatga qaytadi.
- **Kodlash:** GSM-7 (160/153) yoki UCS-2 (70/67; kirill va o'zbek `ʻ`), uzun SMS avtomatik qismlarga bo'linadi (PDU, UDH).
- **Yetkazish hisoboti:** `AT+CNMI` + status report (`+CDS`/`+CDSI`); operator qo'llab-quvvatlasa `delivered` bo'ladi.

## 7. Testlar

```bash
pip install -r requirements.txt
python -m pytest -q          # PDU, mock modem, hosting bilan to'liq oqim, web panel (29 ta test)
```

## 8. Ma'lum cheklovlar

- Haqiqiy modem bilan sinash zarur: modellarning AT/USSD javob formatlari farq qilishi mumkin (Huawei E173/E3372/E1550, ZTE MF-seriyasi, SIM800/900 uchun mo'ljallangan).
- USSD kodlari operatorga bog'liq; sozlamada o'zgartiriladi.
- Operatorlar oddiy SIM dan ommaviy SMS ni cheklaydi — kunlik limit va pauzani saqlang, asosan OTP/tranzaksion xabarlar uchun ishlating.
