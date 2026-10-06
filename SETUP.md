# Self-Healing Framework - Quick Setup

## 1. Install Dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

## 2. Configure

### Update `.env` file:
```
OPENAI_API_KEY=sk-proj-YOUR_KEY_HERE
TEST_EMAIL=test1@automationexercise.com
TEST_PASSWORD=Test@12345
```

### Update `config/apps/automationexercise.yaml`:
```yaml
execution:
  headless: false  # Browser will show
```

## 3. Register Test Account

Go to https://www.automationexercise.com and register:
- Email: `test1@automationexercise.com`
- Password: `Test@12345`

## 4. Add OpenAI Credits

- Go to https://platform.openai.com/account/billing/overview
- Add payment method
- Add credits ($5+)

## 5. Run Tests

```bash
selfheal run suites/tests.xlsx
```

Browser will open and tests will run! ✓

## Folder Structure

```
selfheal_clean/
├── src/                    ← Framework code
├── config/                 ← Configuration files
├── suites/                 ← Test Excel files
├── data/                   ← Database & logs (auto-created)
├── requirements.txt        ← Dependencies
├── pyproject.toml         ← Project config
├── .env                   ← API keys (update this)
└── SETUP.md              ← This file
```

## Results

After running tests:
```
data/
├── store/
│   └── elements.db        ← Learned elements
├── logs/
│   ├── run_history.json
│   └── llm_calls.json
└── runs/
    └── run_XXXXX/
        ├── screenshots/   ← Test screenshots
        └── results.json
```

## Troubleshooting

**Browser not opening?**
- Check `headless: false` in config
- Check Chromium: `playwright install chromium`

**Tests failing?**
- Check OpenAI API key and credits
- Check test account registered

**Screenshots not saving?**
- Check `screenshot_on_pass: true` in config
- Browser must open first

Done! 🎯
