# הרצה אוטומטית בענן עם GitHub Actions

המטרה: הסקריפט ירוץ כל יום בענן של GitHub — **בלי שהמחשב שלך צריך להיות דלוק.**

הקוד כבר מוכן ב-git מקומית עם commit ראשון. נשארו 4 שלבים שרק אתה יכול לעשות
(הם דורשים את חשבון GitHub והסיסמה שלך).

---

## שלב 1 — חשבון GitHub + repo פרטי

1. אם אין לך חשבון: היכנס ל-https://github.com/signup והירשם.
2. צור repository חדש: https://github.com/new
   - **Repository name:** `esim-price-scraper` (או כל שם)
   - בחר **Private** (חשוב! שלא יהיה ציבורי)
   - **אל תסמן** "Add a README" / "Add .gitignore" (כבר יש לנו)
   - לחץ **Create repository**
3. בעמוד שייפתח, העתק את כתובת ה-repo. היא נראית כך:
   `https://github.com/USERNAME/esim-price-scraper.git`

---

## שלב 2 — העלאת הקוד (push)

פתח את אפליקציית **Terminal** במק והדבק (החלף `USERNAME` ואת שם ה-repo בשלך):

```bash
cd "/Users/bhmis/Documents/price update sheets"
git remote add origin https://github.com/USERNAME/esim-price-scraper.git
git branch -M main
git push -u origin main
```

- כשיבקש **Username** — הקלד את שם המשתמש שלך ב-GitHub.
- כשיבקש **Password** — זה **לא** הסיסמה הרגילה! צריך *Personal Access Token*:
  1. היכנס ל-https://github.com/settings/tokens
  2. **Generate new token** → **Tokens (classic)**
  3. סמן את ההרשאה **repo**
  4. **Generate token**, העתק אותו, והדבק אותו כסיסמה ב-Terminal.

---

## שלב 3 — הוספת ה-credentials כ-Secret מאובטח

הקובץ `credentials.json` **לא** עולה ל-GitHub (מטעמי אבטחה). במקום זה שומרים
אותו כ-secret מוצפן:

1. ב-repo ב-GitHub: **Settings** → **Secrets and variables** → **Actions**
2. לחץ **New repository secret**
   - **Name:** `GOOGLE_CREDENTIALS_JSON` (בדיוק כך!)
   - **Secret:** הדבק את **כל** התוכן של `credentials.json`.
     כדי להעתיק אותו בקלות, הרץ ב-Terminal:
     ```bash
     pbcopy < "/Users/bhmis/Documents/price update sheets/credentials.json"
     ```
     ואז פשוט הדבק (Cmd+V) בשדה ה-Secret.
3. לחץ **Add secret**.

---

## שלב 4 — הפעלה ובדיקה

1. ב-repo: לשונית **Actions**.
2. אם מופיעה בקשה לאשר workflows — אשר ("I understand my workflows, enable them").
3. בחר את ה-workflow **"eSIM price scrape"** → **Run workflow** → **Run workflow**.
4. חכה ~5 דקות, פתח את הריצה וראה שהיא ירוקה ✅ ושהטבלה ב-Google Sheets התעדכנה.

מעכשיו הוא ירוץ **אוטומטית כל יום ב-07:00 UTC** = 10:00 בקיץ / 09:00 בחורף
(שעון ישראל). לשינוי השעה — ערוך את שורת ה-`cron` בקובץ
`.github/workflows/scrape.yml`.

---

## שלב 5 (אחרי שווידאת שזה עובד!) — כיבוי ה-launchd המקומי

רק **אחרי** שראית ריצה ירוקה ב-GitHub והטבלה התעדכנה, כבה את האוטומציה המקומית
כדי ששתיהן לא ירוצו במקביל:

```bash
launchctl unload ~/Library/LaunchAgents/com.nitzan.esimprice.plist
```

⚠️ אל תכבה את ה-launchd לפני ששלב 4 עבד — אחרת תישאר בלי כלום (כמו שקרה קודם).
