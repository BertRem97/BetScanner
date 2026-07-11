# Value Bet Scanner

Een professionele value bet scanner met:

- **Multi-API key support** met automatische rotatie
- **Fractional Kelly** stake berekening (standaard 25% Kelly)
- **Median-based EV** voor nauwkeurigere berekeningen
- **Direct betslip links** naar de juiste outcome
- Telegram commando's (/run, /stop, /profit, /set, /keys, /bankroll)
- Google Sheets logging met settlement tracking

## Installatie

```bash
pip install -r requirements.txt
cp .env.template .env
# Vul .env in met je API keys
```

## Configuratie

### OddsPapi API Keys (VERPLICHT)
```bash
# Enkele key:
ODDSPAPI_KEY=jouw_api_key

# Meerdere keys (load balancing):
ODDSPAPI_KEYS=key1,key2,key3
```

Met meerdere keys wordt de load automatisch verdeeld. Gebruik `/keys` in Telegram om het usage te bekijken.

### Telegram Bot
1. @BotFather -> `/newbot`
2. Kopieer token
3. Vind chat_id via `api.telegram.org/bot<TOKEN>/getUpdates`

```
TELEGRAM_BOT_TOKEN=123456789:ABC...
TELEGRAM_CHAT_ID=123456789
```

### Google Sheets
```
GOOGLE_CREDENTIALS_PATH=path/to/service-account.json
GOOGLE_SPREADSHEET_ID=1abc123...
```

Maak een "Settings" sheet met:
```
A          B
bankroll   1000.00
```

## Stake Berekening (Fractional Kelly)

Standaard wordt **quarter Kelly (25%)** gebruikt:

```
Full Kelly = (p * b - q) / b
waar b = odds - 1, p = true probability, q = 1 - p

Stake = Bankroll * Kelly * Fraction (0.25)
```

Configuratie:
```bash
KELLY_FRACTION=0.25  # quarter Kelly (aanbevolen)
KELLY_FRACTION=0.50  # half Kelly (aggressiever)
KELLY_FRACTION=1.00  # full Kelly (zeer aggressief)
```

## EV Berekening (Median)

In plaats van gemiddelde odds wordt de **median** gebruikt:

```python
# Verzamel odds van alle sharp bookmakers
odds_list = [pinnacle_odds, cashpoint_odds, bwin_odds, ...]

# Gebruik median (meer robuust tegen outliers)
median_odds = statistics.median(odds_list)

# Bereken EV
true_probability = 1 / median_odds
EV = (true_probability * soft_odds) - 1
```

## Bookmakers

**Soft (bets plaatsen):** cashpoint, unibet, betano, ladbrokes, bcgame, pinnacle, bwin.be

**Sharp (EV berekening):** pinnacle, cashpoint, bwin.be, ladbrokes, bcgame, unibet, betano

## Telegram Commando's

| Commando | Functie |
|----------|---------|
| `/run` | Start scanning |
| `/stop` | Stop scanning |
| `/profit` | Toon winst/verlies |
| `/bankroll` | Toon bankroll |
| `/keys` | API key usage |
| `/set` | Update settlements |
| `/help` | Hulp |

## Notificatie Formaat

```
*Value Bet Detected!*

Arsenal vs Chelsea
Start: 2026-07-10T15:00:00
League: Premier League

Market: 1X2 (Full Time Result)
Outcome: Home

Soft: cashpoint @ 2.35
Sharp: pinnacle @ 2.200

*EV: 6.82%*
*Win Probability: 45.5%*

*Stake: 10.57*
(Kelly: 1.06% of 1000 bankroll)

[Betslip](https://cashpoint.com/...#home)
```

De betslip link gaat direct naar de geselecteerde outcome.

## Gebruik

```bash
# Interactief (met Telegram commando's)
python value_bet_scanner.py --interactive

# Continue scan
python value_bet_scanner.py --continuous

# Eenmalige scan
python value_bet_scanner.py
```

## Settlement Tracking

Bij elke run of via `/set`:
- API endpoint `/v4/settlements` wordt aangeroepen
- Results: WIN, LOSE, UNDECIDED
- Wordt automatisch bijgewerkt in spreadsheet

## Google Sheets Kolommen

| Kolom | Data |
|-------|------|
| A | Datum |
| B | Start wedstrijd |
| C | Fixture ID |
| D | Match |
| E | Market |
| F | Outcome |
| G | Land |
| H | League |
| I | Soft Book |
| J | Sharp Ref |
| K-O | Quotering 2-3, Hinge, etc. |
| N | Stake Amount |
| O | Bankroll |
| P | EV % |
| Q | Win Probability |
| R | Betslip URL |
| S | Settlement |

## API Rates

OddsPapi free tier:
- 250 requests/key/maand
- Met 3 keys: 750 requests/maand

## Support

OddsPapi docs: https://oddspapi.io/us/docs
