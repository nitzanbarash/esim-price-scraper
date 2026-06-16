# eSIM Price Tracker - Setup Guide

## שלב 1: הגדרת Google Sheets API

### 1.1 צור Google Cloud Project
1. לך ל: https://console.cloud.google.com
2. **Create New Project** → שם: "eSIM Price Tracker"
3. חכה שיווצר

### 1.2 הפעל Google Sheets API
1. חפש "Google Sheets API"
2. לחץ **Enable**
3. חפש "Google Drive API"
4. לחץ **Enable**

### 1.3 יצור Service Account
1. בסרגל בצד: **Service Accounts**
2. **Create Service Account**
   - Name: "esim-scraper"
   - ID: auto-generate
   - Description: "eSIM price scraper"
   - לחץ **Create and Continue**

### 1.4 צור JSON Key
1. בטאב **Keys**
2. **Add Key** → **Create new key**
3. בחר **JSON**
4. לחץ **Create**
5. קובץ `service-account-key.json` יורד
6. **העתק את הקובץ לתיקייה של הפרויקט**: 
   ```
   /Users/bhmis/Documents/price update sheets/
   ```
   (שנה שם ל`credentials.json`)

### 1.5 אפשר ל-Service Account גישה ל-Google Sheet
1. העתק את ה-**client_email** מה-JSON file
2. בGoogleSheet שלך (https://docs.google.com/spreadsheets/d/108D3BUV-MNcIuRZuKUgb-E-b1Ra8moxWZZyI5JxnyRo/edit)
3. לחץ **Share** (כפתור ימני עליון)
4. הדבק את ה-email
5. בחר **Editor**
6. לחץ **Share**

---

## שלב 2: הכן את Google Sheet

### מבנה הטבלה:
```
A: Product Name (שם חבילה/מדינה)
B: URL (https://esim.dog/...)
C: Previous Price (מחיר קודם)
D: Current Price (מחיר נוכחי - עדכון אוטומטי)
E: Last Updated (זמן העדכון - אוטומטי)
F: Price Changed? (האם המחיר השתנה - אוטומטי)
```

### דוגמה:
```
Product Name | URL | Previous Price | Current Price | Last Updated | Price Changed?
Japan 3GB 14 Days | https://esim.dog/jp?tab=fixedgb&data=3&validity=14 | | | |
Thailand 10GB + VPN | https://esim.dog/th?tab=fixedgb&data=10&validity=14&vpn=true | | | |
Japan 1GB 1 Day | https://esim.dog/jp?tab=fixedgb&data=1&validity=1 | | | |
```

---

## שלב 3: התקן Dependencies

```bash
cd /Users/bhmis/Documents/price\ update\ sheets
pip install -r requirements.txt
playwright install chromium
```

---

## שלב 4: בדוק שהקוד עובד

```bash
cd /Users/bhmis/Documents/price\ update\ sheets
python esim_price_scraper.py
```

**צפוי output:**
```
✓ Google Sheets API connected

🔗 Processing: https://esim.dog/jp?tab=fixedgb&data=3&validity=14
  📌 Type 1: Simple URL
  ✓ Price: $5.99

🔗 Processing: https://esim.dog/th?tab=fixedgb&data=10&validity=14&vpn=true
  📌 Type 2: VPN Checkbox URL
  🔴 VPN is enabled - disabling...
  ✓ VPN disabled
  ✓ Price: $12.99

... וכו'

📊 Updating Google Sheets...
  ✓ Updated 3 rows

✅ Done!
```

---

## שלב 5: הגדר Scheduled Routine (יומי בשעה 05:00)

### Option A: cron (Mac/Linux)
```bash
crontab -e
```
הוסף בסוף:
```
0 5 * * * /usr/bin/python3 /Users/bhmis/Documents/price\ update\ sheets/esim_price_scraper.py
```

### Option B: Scheduled Task ב-Claude Code
```
/schedule run /Users/bhmis/Documents/price\ update\ sheets/esim_price_scraper.py daily at 05:00
```

---

## שלב 6: הוסף URLs חדשים

פשוט הוסף בטבלה בשורה חדשה:
- עמודה A: שם החבילה
- עמודה B: URL מ-esim.dog

**הקוד יקרא את כל ה-URLs ויעדכן את כל המחירים אוטומטית!**

---

## טיפול בעיות

### "credentials.json not found"
- וודא שהעתקת את הקובץ לתיקייה הנכונה
- וודא שהשם הוא בדיוק `credentials.json`

### "Google Sheets API not connected"
- בדוק שאפשרת את API בGCP
- בדוק שהקובץ בעל ה-credentials תקין

### Script לא קורא את ה-URLs
- וודא שה-URLs הם בעמודה B
- וודא שיש כותרות בשורה 1 (Product Name, URL, וכו')

---

## שלבים הבא:
1. בצע את שלב 1-3 (Google Setup + Dependencies)
2. בדוק שלב 4 (Run Script)
3. הוסף URLs ל-Sheet
4. הגדר שלב 5 (Scheduled Routine)

✅ זהו! אתה מוכן!
