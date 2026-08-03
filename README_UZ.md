# Follow-up eslatma boti

Erkin matn/ovoz → AI ajratadi (ism, tel, sana, izoh) → tasdiq → saqlaydi.
Har kuni REPORT_HOUR (9:00) da o'sha kungi eslatmalar keladi.

## Railway sozlamalari (Variables)
- `BOT_TOKEN` — @BotFather tokeni
- `GEMINI_API_KEY` — Google Gemini kaliti
- `GEMINI_MODEL` — (ixtiyoriy) default gemini-2.5-flash
- `REPORT_HOUR` — (ixtiyoriy) default 9
- `TZ` — Asia/Tashkent
- `DATA_DIR` — /data  (Railway'da Volume qo'shing, /data ga ulang — saqlanib qolishi uchun)
- `ESLATMA_ADMINS` — (ixtiyoriy) vergul bilan ruxsatli chat id'lar; bo'sh = hamma

## Ishlatish
Botga yozing yoki ayting: «Akmal 998901234567 8-avgust lesa olmoqchi»
→ "Shunday tushundim… [✅ Saqlash] [✏️ Bekor]"

Buyruqlar: /royxat, /bugun, /ochir <id>

## MUHIM
Volume qo'shing (Railway → service → Volume → /data), aks holda qayta deployda eslatmalar o'chadi.
