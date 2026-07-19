#!/usr/bin/env python3
"""
Value Bet Scanner - OddsPapi API Integration
Features:
- Multi-API key support with rotation
- Fractional Kelly stake calculation
- Median-based EV calculation (more accurate)
- Direct betslip links to outcomes
- Telegram commands (/run, /stop, /profit, /set, /keys, /bankroll, /manueel)
- Manual bet entry via Telegram conversation flow
- Settlement tracking via API
- Google Sheets logging with monthly tabs
"""
import re
import requests
import json
import time
import logging
import threading
import statistics
import subprocess
import random
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
import os
from pathlib import Path
from collections import defaultdict
from pprint import pprint
from mapping import MARKETS as mapping

from dotenv import load_dotenv
load_dotenv()

try:
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    GOOGLE_SHEETS_AVAILABLE = True
except ImportError:
    GOOGLE_SHEETS_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('value_bet_scanner.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Column headers for the bet log sheet
SHEET_HEADERS = [
    'Datum', 'Start wedstrijd', 'Event fixture', 'Match',
    'Sport', 'Market', 'Outcome', 'Land / Tournooi', 'League',
    'Soft Book', 'Odds overzicht (soft)', 'Sharp Ref (mediaan)',
    'EV %', 'Win Prob', 'Stake Amount', 'Kelly %',
    'Betslip', 'Mogelijke winst', 'Settlement'
]

class ApiKeyManager:
    """Manager for multiple API keys with rotation and rate limiting"""

    def __init__(self, api_keys: List[str], requests_per_key: int = 250):
        self.api_keys = api_keys if isinstance(api_keys, list) else [api_keys]
        self.requests_per_key = requests_per_key
        self.current_index = 0
        self.key_usage = {key: 0 for key in self.api_keys}
        self.key_errors = {key: 0 for key in self.api_keys}
        self.total_requests = 0
        self._lock = threading.Lock()
    

    def get_next_key(self) -> str:
        with self._lock:
            for _ in range(len(self.api_keys)):
                key = self.api_keys[self.current_index]
                self.current_index = (self.current_index + 1) % len(self.api_keys)
                if self.key_usage[key] < self.requests_per_key:
                    return key
            return min(self.api_keys, key=lambda k: self.key_errors[k])

    def record_request(self, api_key: str):
        with self._lock:
            self.key_usage[api_key] = self.key_usage.get(api_key, 0) + 1
            self.total_requests += 1

    def record_error(self, api_key: str):
        with self._lock:
            self.key_errors[api_key] = self.key_errors.get(api_key, 0) + 1

    def get_status(self) -> Dict:
        with self._lock:
            return {
                'total_requests': self.total_requests,
                'keys': [
                    {
                        'key': key[:8] + '...' if len(key) > 8 else key,
                        'usage': self.key_usage.get(key, 0),
                        'limit': self.requests_per_key,
                        'errors': self.key_errors.get(key, 0),
                        'remaining': max(0, self.requests_per_key - self.key_usage.get(key, 0))
                    }
                    for key in self.api_keys
                ],
                'total_remaining': sum(
                    max(0, self.requests_per_key - self.key_usage.get(key, 0))
                    for key in self.api_keys
                )
            }


@dataclass
class ValueBet:
    """Represents a detected value bet"""
    fixture_id: str
    participant1: str
    participant2: str
    start_time: str
    tournament_name: str
    category_name: str
    market: str
    market_id: str
    outcome: str
    outcome_id: str
    sharp_bookmaker: str
    sharp_odds: float          # median sharp reference
    soft_bookmaker: str        # best soft book for this bet
    soft_odds: float           # odds at best soft book
    soft_bookmaker_odds: Dict[str, float]  # odds at ALL soft books for this outcome
    ev_percentage: float
    win_probability: float
    sport: str
    stake_amount: float
    bankroll: float
    kelly_fraction: float
    timestamp: str
    betslip_url: Optional[str] = None
    settlement_status: str = "PENDING"

    def to_dict(self) -> Dict:
        # Build a compact odds overview string: "cashpoint:2.10 unibet:2.05 ..."
        odds_str = '  '.join(
            f"{bk} @ {o:.2f}"
            for bk, o in sorted(self.soft_bookmaker_odds.items(), key=lambda x: -x[1])
        )

        return {
            'Datum': self.timestamp,
            'Start wedstrijd': self.start_time,
            'Event fixture': self.fixture_id,
            'Match': f"{self.participant1} - {self.participant2}",
            'Sport': self.sport,
            'Market': self.market,
            'Outcome': self.outcome,
            'Land / Tournooi': self.category_name,
            'League': self.tournament_name,
            'Soft Book': f"{self.soft_bookmaker} @ {self.soft_odds}",
            'Odds overzicht (soft)': odds_str,
            'Sharp Ref (mediaan)': f"{self.sharp_odds:.3f}",
            'EV %': f"{self.ev_percentage:.2f}%",
            'Win Prob': f"{self.win_probability:.1%}",
            'Stake Amount': f"{self.stake_amount:.2f}",
            'Kelly %': f"{self.kelly_fraction:.2%}",
            'Betslip': self.betslip_url or '',
            'Mogelijke winst': f"{self.soft_odds * self.stake_amount - self.stake_amount:.2f}",
            'Settlement': self.settlement_status
        }


class OddsPapiClient:
    """Client for OddsPapi API v4 with multi-key support"""

    BASE_URL = "https://api.oddspapi.io/v4"


    SOFT_BOOKMAKERS = [#cashpoint.be --> betcenter.be
        'cashpoint', 'unibet', 'pinnacle', 'goldenpalacesports.be',
        'bcgame', 'bwin.be', 'napoleonsports.be'

        #ladbrokes.be
        #betcenter.be, 
        # bingoal.be, 
        # betfirst.be, 
        #goldenpalacesports.be, 
    ]

    # Sharp books used only for median reference, NOT as bet targets
    SHARP_BOOKMAKERS = [
        'pinnacle', 'unibet', 'sbobet', 'betano', 'bwin.be'
    ]

    def __init__(self, api_keys, settlements, requests_per_key: int = 250):
        self.key_manager = ApiKeyManager(api_keys, requests_per_key)
        self.session = requests.Session()
        self.settlements = settlements
        self.api_keys = api_keys


    def rotate_ip(self):
        try:
            subprocess.run(
                ["bash", "/home/pi/services/BetScanner/rotate_vpn_on_call.sh"],
                check=True
            )
        except subprocess.CalledProcessError as e:
            print(f"VPN-script faalde: {e}")
        except KeyboardInterrupt:
            print("Programma onderbroken door gebruiker")


    def _make_request(self, endpoint: str, params: Dict = None) -> requests.Response:

        api_key = self.key_manager.get_next_key()
        if params is None:
            params = {}
        params['apiKey'] = api_key

        try:
            response = self.session.get(
                f"{self.BASE_URL}/{endpoint}",
                params=params,
                timeout=(10, 60)
            )
            
            if response.status_code == 429:
                self.key_manager.record_error(api_key)
                
                return self._make_request(endpoint, params)
            
            if response.status_code == 403:

                try:
                    error = response.json().get("error", "")
                except ValueError:
                    error = response.text.strip()

                if error == "Forbidden":
                    logger.warning("Forbidden 403 -> rotate IP address")
                    
                    self.rotate_ip()
                    time.sleep(5)
                    return self._make_request(endpoint, params)
                
            self.key_manager.record_request(api_key)
            return response
        except Exception as e:
            self.key_manager.record_error(api_key)
            raise
    

    def get_key_status(self) -> Dict:
        return self.key_manager.get_status()

    def get_sports(self) -> List[Dict]:
        try:
            response = self._make_request("sports")
            if response is None:
                return None
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error fetching sports: {e}")
            return []

    def get_tournaments(self, sport_id: int = 10) -> List[Dict]:
        try:
            response = self._make_request("tournaments", {'sportId': sport_id})
            if response is None:
                return None
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error fetching tournaments: {e}")
            return []

    def get_fixtures(self, tournament_id: Optional[int] = None, sport_id: int = 10,
                      days_ahead: int = 7, has_odds: bool = True) -> List[Dict]:
        today = datetime.now().date()

        params = {
            'sportId': sport_id,
            'from': today.isoformat(),
            'to': (today + timedelta(days=days_ahead)).isoformat(),
        }
        if tournament_id:
            params['tournamentId'] = tournament_id
        if has_odds:
            params['hasOdds'] = 'true'

        try:
            response = self._make_request("fixtures", params)
            if response is None:
                return None
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error fetching fixtures: {e}")
            return []

    def get_odds(self, fixture_id: str) -> Dict:
        try:
            response = self._make_request("odds", {'fixtureId': fixture_id})
            if response is None:
                return None
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error fetching odds for {fixture_id}: {e}")
            return {}
        
  
    def get_settlements(self, fixture_ids: List[str]) -> List[Dict]:
        if not fixture_ids:
            return []
        
        try:
            for id in fixture_ids:
                for x in self.settlements:
                    if id == x["fixtureId"]:
                        continue
                
                response = self._make_request("settlements", {'fixtureId': id})
                if response is None:
                    return None
                response.raise_for_status()
                self.settlements.append(response.json())

        except Exception as e:
            logger.error(f"Error fetching settlements: {e}")
            return []


    def extract_odds_from_market(
        self,
        bookmaker_data: Dict,
        market_ids: List[str]
    ) -> Dict[str, Dict[str, float]]:

        odds = {}

        markets = bookmaker_data.get('markets', {})

        for market_id in market_ids:
            market = markets.get(market_id, {})
            outcomes = market.get('outcomes', {})

            market_odds = {}

            for outcome_id, outcome_data in outcomes.items():
                players = outcome_data.get('players', {})

                if '0' in players:
                    price = players['0'].get('price')

                    if price:
                        market_odds[outcome_id] = price

            if market_odds:
                odds[market_id] = market_odds

        return odds



    def get_outcome_betslip_url(self, bookmaker_data: Dict, outcome_id: str) -> Optional[str]:
        fixture_path = bookmaker_data.get('fixturePath', '')
        if not fixture_path:
            return None

        markets = bookmaker_data.get('markets', {})
        market_101 = markets.get('101', {})
        outcomes = market_101.get('outcomes', {})
        outcome_data = outcomes.get(outcome_id, {})
        players = outcome_data.get('players', {})
        player_0 = players.get('0', {})
        bookmaker_outcome_id = player_0.get('bookmakerOutcomeId', '')

        if bookmaker_outcome_id:
            return fixture_path
        
        return None


class ValueBetCalculator:
    """Calculate value bets using median sharp reference and fractional Kelly"""


    def __init__(self, min_ev_threshold: float = 2.0, kelly_fraction: float = 0.25):
        self.min_ev_threshold = min_ev_threshold
        self.kelly_fraction = kelly_fraction
        self.odds_client = None

    def set_odds_client(self, client: OddsPapiClient):
        self.odds_client = client

    def calculate_implied_probability(self, odds: float) -> float:
        return 1 / odds if odds > 0 else 0

    def calculate_ev(self, soft_odds: float, sharp_odds: float) -> float:
        if sharp_odds <= 0 or soft_odds <= 0:
            return 0
        true_probability = self.calculate_implied_probability(sharp_odds)
        return ((true_probability * soft_odds) - 1) * 100

    def calculate_kelly(self, probability: float, odds: float) -> float:
        if odds <= 1:
            return 0
        b = odds - 1
        q = 1 - probability
        kelly = (probability * b - q) / b
        return max(0, kelly)

    def calculate_stake(self, probability: float, odds: float, bankroll: float,
                        fraction: float = 0.25) -> Tuple[float, float]:
        full_kelly = self.calculate_kelly(probability, odds)
        fractional_kelly = full_kelly * fraction
        stake_amount = bankroll * fractional_kelly
        return stake_amount, fractional_kelly

    def calculate_median_odds(self, odds_list: List[float]) -> float:
        if not odds_list:
            return 0
        return statistics.median(odds_list)

    def analyze_fixture(self, fixture: Dict, odds_data: Dict, bankroll: float) -> List[ValueBet]:
        value_bets = []
        bookmaker_odds = odds_data.get('bookmakerOdds', {})

        sport_id = str(fixture.get('sportId'))
        sport_data = mapping.get(sport_id, {})

        if not sport_data:
            return value_bets

        sport_name = next(iter(sport_data))
        sport_markets = sport_data[sport_name]
        market_ids = list(sport_markets.keys())
             
        # Collect median odds from sharp bookmakers
        sharp_prices_by_outcome: Dict[str, Dict[str, Dict[str, float]]] = {}
        for sharp in OddsPapiClient.SHARP_BOOKMAKERS:
            if sharp not in bookmaker_odds:
                continue

            markets = self.odds_client.extract_odds_from_market(
                bookmaker_odds[sharp],
                market_ids
                )
            
            for market_id, market_odds in markets.items():
                for outcome_id, price in market_odds.items():
                    sharp_prices_by_outcome \
                        .setdefault(market_id, {}) \
                        .setdefault(outcome_id, {})[sharp] = price \


        if not sharp_prices_by_outcome:
            return value_bets

        median_sharp_odds = {}

        for market_id, outcomes in sharp_prices_by_outcome.items():
            median_sharp_odds[market_id] = {}

            for outcome_id, prices in outcomes.items():
                sharp_odds_list = list(prices.values())
                median_sharp_odds[market_id][outcome_id] = \
                self.calculate_median_odds(sharp_odds_list)
             
      
        soft_odds_by_outcome: Dict[str, Dict[str, float]] = {}
        for soft_book in OddsPapiClient.SOFT_BOOKMAKERS:
            if soft_book not in bookmaker_odds:
                continue

            book_odds = self.odds_client.extract_odds_from_market(
                bookmaker_odds[soft_book], market_ids
            )

            for market_id, market_odds in book_odds.items():
                for outcome_id, price in market_odds.items():
                    soft_odds_by_outcome \
                        .setdefault(market_id, {}) \
                        .setdefault(outcome_id, {})[soft_book] = price \


        # Find value: for each outcome pick the best soft book

        for market_id, outcomes in median_sharp_odds.items():
            for outcome_id, median_sharp in outcomes.items():

                all_soft = (
                    soft_odds_by_outcome
                    .get(market_id, {})
                    .get(outcome_id, {})
                )

                if not all_soft:
                    continue

                best_book = max(all_soft, key=lambda b: all_soft[b])
                best_odds = all_soft[best_book]
                ev = self.calculate_ev(best_odds, median_sharp)

                if ev >= self.min_ev_threshold:
                    win_prob = self.calculate_implied_probability(median_sharp)
                    stake_amount, kelly_pct = self.calculate_stake(
                        win_prob, best_odds, bankroll, self.kelly_fraction
                    )
                    betslip_url = self.odds_client.get_outcome_betslip_url(
                        bookmaker_odds[best_book], outcome_id
                    )

                    market_info = sport_markets.get(market_id)

                    if not market_info:
                        continue

                    market_name = next(iter(market_info))
                    outcomes = market_info[market_name]

                    outcome_name = outcomes.get(
                        outcome_id,
                        "Unknown"
                    )


                    value_bets.append(ValueBet(
                        fixture_id=fixture.get('fixtureId', ''),
                        participant1=fixture.get('participant1Name', 'Unknown'),
                        participant2=fixture.get('participant2Name', 'Unknown'),
                        start_time=fixture.get('startTime', ''),
                        tournament_name=fixture.get('tournamentName', 'Unknown'),
                        category_name=fixture.get('categoryName', 'Unknown'),
                        market=market_name,
                        market_id=market_id,
                        outcome = outcome_name,
                        outcome_id=outcome_id,
                        sharp_bookmaker='median',
                        sharp_odds=median_sharp,
                        soft_bookmaker=best_book,
                        soft_odds=best_odds,
                        soft_bookmaker_odds=dict(all_soft),
                        ev_percentage=ev,
                        sport=sport_name,
                        win_probability=win_prob,
                        stake_amount=stake_amount,
                        bankroll=bankroll,
                        kelly_fraction=kelly_pct,
                        timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        betslip_url=betslip_url
                    ))

        return value_bets


class GoogleSheetsManager:
    """Manage Google Sheets with monthly tab support"""

    SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
    HEADER_ROW = SHEET_HEADERS

    def __init__(self, credentials_path: str, spreadsheet_id: str):
        self.spreadsheet_id = spreadsheet_id
        self.available = False
        self.service = None
        self._sheet_lock = threading.Lock()
        # In-process cache: set of sheet names known to exist
        self._known_sheets: Optional[set] = None
        self.first_data_row = 12

        if not GOOGLE_SHEETS_AVAILABLE:
            logger.warning("Google Sheets libraries not installed")
            return

        try:
            credentials = Credentials.from_service_account_file(credentials_path, scopes=self.SCOPES)
            self.service = build('sheets', 'v4', credentials=credentials)
            self.available = True
            logger.info("Google Sheets client initialized")
        except Exception as e:
            logger.error(f"Error initializing Google Sheets: {e}")


    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------
    def update_main_sheet_totals(self, main_sheet="Dashboard"):

        try:
            spreadsheet = self.service.spreadsheets().get(
                spreadsheetId=self.spreadsheet_id
            ).execute()

            sheets = spreadsheet.get("sheets", [])

            pattern = re.compile(r"^\d{4}-\d{2}$")

            monthly_sheets = [
                s["properties"]["title"]
                for s in sheets
                if pattern.match(s["properties"]["title"])
            ]

            len_monthly_sheets = len(monthly_sheets)

            # 5 rijen voor B2:B6 en 5 rijen voor D2:D6
            total_B = [0, 0, 0, 0, 0]
            total_D = [0, 0, 0, 0, 0]


            for sheet_name in monthly_sheets:

                # Kolom B ophalen
                result_B = self.service.spreadsheets().values().get(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"{sheet_name}!B2:B6"
                ).execute()

                values_B = result_B.get("values", [])

                for i, row in enumerate(values_B):
                    if i >= len(total_B):
                        break

                    if len(row) == 0:
                        continue

                    try:
                        total_B[i] += float(row[0])
                    except (ValueError, TypeError):
                        continue

                # Kolom D ophalen
                result_D = self.service.spreadsheets().values().get(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"{sheet_name}!D2:D6"
                ).execute()

                values_D = result_D.get("values", [])

                for i, row in enumerate(values_D):
                    if i >= len(total_D):
                        break

                    if len(row) == 0:
                        continue

                    try:
                        total_D[i] += float(row[0])
                    except (ValueError, TypeError):
                        continue

            # Resultaat samenvoegen voor Dashboard A2:D6
            output = []

            for i in range(5):
                output.append([
                    "",              # A behoudt beschrijving
                    total_B[i],      # B totalen
                    "",              # C behoudt beschrijving
                    total_D[i]       # D totalen
                ])


            # Schrijf enkel B2:B6
            self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"{main_sheet}!B2:B6",
                valueInputOption="USER_ENTERED",
                body={
                    "values": [[value] for value in total_B]
                }
            ).execute()

            # Schrijf enkel D2:D6
            self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"{main_sheet}!D2:D6",
                valueInputOption="USER_ENTERED",
                body={
                    "values": [[value] for value in total_D]
                }
            ).execute()


            # Aantal maandbladen opslaan
            self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"{main_sheet}!B14",
                valueInputOption="USER_ENTERED",
                body={
                    "values": [[len_monthly_sheets]]
                }
            ).execute()


            logger.info("Main sheet totals updated")
            return "Dashboard totals succesvol bijgewerkt"

        except Exception as e:
            logger.error(f"Error updating totals: {e}")
            return "Dashboard totals niet kunnen bijwerken"

    def _fetch_sheet_meta(self) -> List[Dict]:
        """Single API call — returns the sheets array from spreadsheet metadata."""
        meta = self.service.spreadsheets().get(
            spreadsheetId=self.spreadsheet_id
        ).execute()
        return meta.get('sheets', [])

    def _refresh_known_sheets(self) -> Dict[str, int]:
        """Returns {title: sheetId} and updates _known_sheets cache."""
        sheets = self._fetch_sheet_meta()
        mapping = {s['properties']['title']: s['properties']['sheetId'] for s in sheets}
        self._known_sheets = set(mapping.keys())
        return mapping

    def _duplicate_sheet(self, source_id: int, new_name: str) -> bool:
        """Copy sheet by id to new_name and clear data rows (keep header)."""
        try:
            self.service.spreadsheets().sheets().copyTo(
                spreadsheetId=self.spreadsheet_id,
                sheetId=source_id,
                body={'destinationSpreadsheetId': self.spreadsheet_id}
            ).execute()
            # The copy lands as "Copy of TEMPLATE" — look it up fresh
            mapping = self._refresh_known_sheets()
            copy_name = next(
                (t for t in mapping if t.startswith('Kopie van ') and t not in (new_name,)),
                None
            )
            if copy_name is None:
                logger.error("Could not find the copied sheet to rename")
                return False
            copy_id = mapping[copy_name]
            self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={'requests': [{'updateSheetProperties': {
                    'properties': {'sheetId': copy_id, 'title': new_name},
                    'fields': 'title'
                }}]}
            ).execute()
            # Clear data rows, keep header row
            self.service.spreadsheets().values().clear(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{new_name}'!A{self.first_data_row}:Z"
            ).execute()
            # Update cache
            self._known_sheets.discard(copy_name)
            self._known_sheets.add(new_name)
            return True
        except Exception as e:
            logger.error(f"Error duplicating sheet: {e}")
            return False

    # ------------------------------------------------------------------
    # Monthly sheet management
    # ------------------------------------------------------------------

    

    def get_or_create_monthly_sheet(self, year: int = None, month: int = None) -> str:
        """
        Return the sheet name for the given month (default: current month).
        Creates exactly one new sheet from the 'TEMPLATE' tab if needed.
        Sheet names follow the pattern 'YYYY-MM' (e.g. '2026-07').
        """
        if not self.available:
            return 'Sheet1'

        now = datetime.now()
        year = year or now.year
        month = month or now.month
        sheet_name = f"{year}-{month:02d}"

        with self._sheet_lock:
            # Fast path: already in local cache
            if self._known_sheets is not None and sheet_name in self._known_sheets:
                return sheet_name

            # Single API call to get current state
            mapping = self._refresh_known_sheets()

            # Second check after refresh (handles race on startup)
            if sheet_name in mapping:
                return sheet_name

            # Try to copy from TEMPLATE
            if 'TEMPLATE' in mapping:
                ok = self._duplicate_sheet(mapping['TEMPLATE'], sheet_name)
                if ok:
                    logger.info(f"Created monthly sheet '{sheet_name}' from TEMPLATE")
                    return sheet_name

            # Fallback: add blank sheet and write header
            try:
                self.service.spreadsheets().batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={'requests': [{'addSheet': {
                        'properties': {'title': sheet_name}
                    }}]}
                ).execute()
                self.service.spreadsheets().values().update(
                    spreadsheetId=self.spreadsheet_id,
                    range=f"'{sheet_name}'!A1",
                    valueInputOption='USER_ENTERED',
                    body={'values': [self.HEADER_ROW]}
                ).execute()
                self._known_sheets.add(sheet_name)
                logger.info(f"Created monthly sheet '{sheet_name}' (blank)")
            except Exception as e:
                logger.error(f"Error creating sheet '{sheet_name}': {e}")

            return sheet_name

    def ensure_template_sheet(self):
        """
        Create a TEMPLATE tab with the correct header if it does not exist.
        """
        if not self.available:
            return
        with self._sheet_lock:
            mapping = self._refresh_known_sheets()
            if 'TEMPLATE' in mapping:
                return
            try:
                self.service.spreadsheets().batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={'requests': [{'addSheet': {
                        'properties': {'title': 'TEMPLATE'}
                    }}]}
                ).execute()
                self.service.spreadsheets().values().update(
                    spreadsheetId=self.spreadsheet_id,
                    range="'TEMPLATE'!A1",
                    valueInputOption='USER_ENTERED',
                    body={'values': [self.HEADER_ROW]}
                ).execute()
                self._known_sheets.add('TEMPLATE')
                logger.info("Created TEMPLATE sheet")
            except Exception as e:
                logger.error(f"Error creating TEMPLATE: {e}")

    # ------------------------------------------------------------------
    # Data operations
    # ------------------------------------------------------------------

    def append_row(self, row: List[str], sheet_name: str = None) -> bool:
        if not self.available:
            return False
        if sheet_name is None:
            sheet_name = self.get_or_create_monthly_sheet()
        try:
            self.service.spreadsheets().values().append(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{sheet_name}'!A{self.first_data_row}",
                valueInputOption='USER_ENTERED',
                insertDataOption='INSERT_ROWS',
                body={'values': [row]}
            ).execute()
            return True
        except Exception as e:
            logger.error(f"Error appending row: {e}")
            return False

    def get_all_rows(self, sheet_range: str = None) -> List[List[str]]:
        if not self.available:
            return []
        if sheet_range is None:
            sheet_name = self.get_or_create_monthly_sheet()
            sheet_range = f"'{sheet_name}'!A:Z"
        try:
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=sheet_range
            ).execute()

            values = result.get("values", [])
            return values
            
        except Exception as e:
            logger.error(f"Error reading sheet: {e}")
            return []

    def update_cell(self, row: int, col: int, value: str,
                    sheet_name: str = None) -> bool:
        
        if not self.available:
            return False
        if sheet_name is None:
            sheet_name = self.get_or_create_monthly_sheet()
        try:
            col_letter = chr(65 + col)
            self.service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{sheet_name}'!{col_letter}{row}",
                valueInputOption='USER_ENTERED',
                body={'values': [[value]]}
            ).execute()
            return True
        
        except Exception as e:
            logger.error(f"Error updating cell: {e}")
            return False

    def get_profit_loss(self) -> Dict:
        rows = self.get_all_rows()
        if not rows:
            return {'total': 0, 'wins': 0, 'losses': 0, 'pending': 0}

        headers = [h.lower().strip() if h else '' for h in rows[10]]
        col_idx = {h: i for i, h in enumerate(headers)}

        total = 0.0
        wins = 0
        losses = 0
        pending = 0

        for row in rows[11:]:
            try:
                settlement = ''
                for key in ['settlement', 'status', 'result']:
                    if key in col_idx and col_idx[key] < len(row):
                        settlement = row[col_idx[key]].upper()
                        break
                if 'WIN' in settlement:
                    wins += 1
                elif 'LOSE' in settlement:
                    losses += 1
                else:
                    pending += 1
            except Exception:
                pending += 1

        return {'total': total, 'wins': wins, 'losses': losses, 'pending': pending}

    def get_bankroll(self) -> float:
        if not self.available:
            print("NOT AVAILABLE")
            return 500
        try:
            result = self.service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range='Dashboard!A1:B20'
            ).execute()
            for row in result.get('values', []):
                if len(row) >= 2 and row[0].lower().strip() == 'bankroll':
                    return float(row[1])
        except Exception:
            pass
        return 500

    def update_settlement(self, fixture_id: str, settlement: str,
                        sheet_name: str = None) -> bool:

        rows = self.get_all_rows(sheet_name)

        header_row = 10  # rij 11 in Sheets

        if len(rows) <= header_row:
            return False

        headers = [
            h.lower() if h else ''
            for h in rows[header_row]
        ]
        settlement_col = None

        for j, h in enumerate(headers):
            if 'settlement' in h:
                settlement_col = j
                break

        if settlement_col is None:
            return False
       
        for i, row in enumerate(rows[header_row + 1:], start=header_row + 1):
            # fixture ID staat in kolom C
            if len(row) > 2 and row[2] == fixture_id:
                # i is de echte index in rows
                return self.update_cell(
                    i + 1,
                    settlement_col,
                    settlement,
                    sheet_name
                )

        return False

# ---------------------------------------------------------------------------
# Manual bet entry state machine
# ---------------------------------------------------------------------------

MANUAL_STEPS = [
    ('match',        'Wedstrijd (bijv. Arsenal - Chelsea):'),
    ('start_time',   'Starttijd (bijv. 2026-07-15 21:00):'),
    ('league',       'Competitie (bijv. Premier League):'),
    ('category',     'Land (bijv. England):'),
    ('sport',        'Soort sport (bijv. Football):'),
    ('market',       'Markt (bijv. 1X2):'),
    ('outcome',      'Uitkomst (bijv. Home / Draw / Away):'),
    ('soft_book',    'Bookmaker (bijv. cashpoint):'),
    ('soft_odds',    'Odds bij bookmaker (bijv. 2.15):'),
    ('sharp_odds',   'Sharp referentie odds (mediaan, bijv. 2.00):'),
]


class ManualBetSession:
    """Tracks the state of an active manual-entry conversation for one chat."""

    def __init__(self, min_ev_threshold: float = 2.0, kelly_fraction: float = 0.25):
        self.step_index = 0
        self.data: Dict[str, str] = {}
        self.min_ev_threshold = min_ev_threshold
        self.kelly_fraction = kelly_fraction
       
    @property
    def current_step(self) -> Optional[Tuple[str, str]]:
        if self.step_index < len(MANUAL_STEPS):
            return MANUAL_STEPS[self.step_index]
        return None
    
    
    def record_answer(self, answer: str):
        key, _ = MANUAL_STEPS[self.step_index]
        self.data[key] = answer.strip()
        self.step_index += 1

    @property
    def is_complete(self) -> bool:
        return self.step_index >= len(MANUAL_STEPS)
    

    def calculate_kelly(self, probability: float, odds: float) -> float:
        if odds <= 1:
            return 0
        b = odds - 1
        q = 1 - probability
        kelly = (probability * b - q) / b
        return max(0, kelly)
    
    def calculate_stake(self, probability: float, odds: float, bankroll: float,
                        fraction: float = 0.25) -> Tuple[float, float]:
        full_kelly = self.calculate_kelly(probability, odds)
        fractional_kelly = full_kelly * fraction
        stake_amount = bankroll * fractional_kelly
        return stake_amount, fractional_kelly

    def to_value_bet(self, bankroll: float) -> 'ValueBet':
        d = self.data
        parts = d['match'].split('-', 1)
        p1 = parts[0].strip()
        p2 = parts[1].strip() if len(parts) > 1 else ''
        soft_odds = float(d['soft_odds'])
        sharp_odds = float(d['sharp_odds'])
        win_prob = 1 / sharp_odds if sharp_odds > 0 else 0
        sport = d['sport']

        ev = ((win_prob * soft_odds) - 1) * 100
        if ev >= self.min_ev_threshold:
            stake, kelly_pct = self.calculate_stake(
                win_prob, soft_odds, bankroll, self.kelly_fraction)

            betslip = d['betslip'] if d['betslip'] != '-' else None
   
            return ValueBet(
                fixture_id=f"manual_{datetime.now().strftime('%Y%m%d%H%M%S')}",
                participant1=p1,
                participant2=p2,
                start_time=d['start_time'],
                tournament_name=d['league'],
                category_name=d['category'],
                market=d['market'],
                market_id='manual',
                outcome=d['outcome'],
                outcome_id='manual',
                sharp_bookmaker='manueel',
                sharp_odds=sharp_odds,
                soft_bookmaker=d['soft_book'],
                soft_odds=soft_odds,
                sport=sport,
                soft_bookmaker_odds={d['soft_book']: soft_odds},
                ev_percentage=ev,
                win_probability=win_prob,
                stake_amount=stake,
                bankroll=bankroll,
                kelly_fraction=kelly_pct,
                timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                betslip_url=betslip,
                settlement_status='PENDING'
            )


class TelegramBot:
    """Telegram bot with commands, notifications, and manual bet entry"""

    def __init__(self, bot_token: str, chat_id: str,
                 sheets: GoogleSheetsManager = None):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.sheets = sheets
        self.pending_bets: Dict[int, ValueBet] = {}
        self.last_update_id = 0
        self._scanner = None
        # Per-chat manual entry sessions
        self._manual_sessions: Dict[str, ManualBetSession] = {}

    def set_scanner(self, scanner):
        self._scanner = scanner

    # ------------------------------------------------------------------
    # Messaging helpers
    # ------------------------------------------------------------------

    def send_message(self, text: str, chat_id: str = None,
                     keyboard: Dict = None) -> Optional[int]:
        cid = chat_id or self.chat_id
        payload: Dict = {
            "chat_id": cid,
            "text": text,
            "parse_mode": "Markdown"
        }
        if keyboard:
            payload["reply_markup"] = keyboard
        try:
            response = requests.post(
                f"{self.base_url}/sendMessage", json=payload
            )
            result = response.json()
            if result.get('ok'):
                return result['result']['message_id']
        except Exception as e:
            logger.error(f"Error sending message: {e}")
        return None

    def edit_message(self, message_id: int, text: str, chat_id: str = None):
        cid = chat_id or self.chat_id
        try:
            requests.post(
                f"{self.base_url}/editMessageText",
                json={
                    "chat_id": cid,
                    "message_id": message_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "reply_markup": {"inline_keyboard": []}
                }
            )
        except Exception:
            pass

    def answer_callback(self, callback_id: str, text: str = ""):
        try:
            requests.post(
                f"{self.base_url}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text}
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Value bet notification
    # ------------------------------------------------------------------

    def _format_odds_table(self, soft_odds: Dict[str, float],
                           best_book: str, sharp_odds: float) -> str:
        """
        Build a compact Markdown odds overview.
        Example:
          cashpoint   2.15 *
          unibet      2.10
          betano      2.05
          ─────────────────
          Sharp ref   2.00 (mediaan)
        """
        lines = []
        for bk, o in sorted(soft_odds.items(), key=lambda x: -x[1]):
            marker = ' ✓' if bk == best_book else ''
            lines.append(f"  `{bk:<12}` {o:.2f}{marker}")
        lines.append(f"  `{'────────────':12}`")
        lines.append(f"  `{'Sharp ref':<12}` {sharp_odds:.3f} (mediaan)")
        return '\n'.join(lines)

    def send_value_bet_notification(self, bet: ValueBet) -> bool:
        odds_table = self._format_odds_table(
            bet.soft_bookmaker_odds, bet.soft_bookmaker, bet.sharp_odds
        )

        betslip_line = f"[Betslip]({bet.betslip_url})" if bet.betslip_url else "_geen betslip URL_"

        message = (
            f"*Value Bet Gevonden!*\n\n"
            f"*{bet.participant1}* vs *{bet.participant2}*\n"
            f"Start: {bet.start_time}\n"
            f"Competitie: {bet.tournament_name} ({bet.category_name})\n"
            f"Sport: {bet.sport}\n\n"
            f"Markt: {bet.market}\n"
            f"Uitkomst: *{bet.outcome}*\n\n"
            f"*Odds overzicht:*\n"
            f"{odds_table}\n\n"
            f"*EV: {bet.ev_percentage:.2f}%*\n"
            f"Win kans: {bet.win_probability:.1%}\n\n"
            f"*Inzet: €{bet.stake_amount:.2f}*\n"
            f"(Kelly: {bet.kelly_fraction:.2%} van {bet.bankroll:.0f})\n\n"
            f"{betslip_line}"
        )

        keyboard = {
            "inline_keyboard": [[
                {"text": "Bevestigen", "callback_data": f"confirm_{bet.fixture_id}_{bet.soft_bookmaker}_{bet.outcome_id}"},
                {"text": "Afwijzen",   "callback_data": f"reject_{bet.fixture_id}"}
            ]]
        }

        msg_id = self.send_message(message, keyboard=keyboard)
        if msg_id is not None:
            self.pending_bets[msg_id] = bet
            return True
        return False

    # ------------------------------------------------------------------
    # Update polling
    # ------------------------------------------------------------------
    
    def get_updates(self, timeout: int = 5) -> List[Dict]:
        try:
            response = requests.get(
                f"{self.base_url}/getUpdates",
                params={
                    "offset": self.last_update_id + 1,
                    "timeout": timeout
                },
                timeout=(10, timeout + 10)   # connect timeout, read timeout
            )

            response.raise_for_status()

            result = response.json()

            if not result.get("ok"):
                logger.warning(f"Telegram API returned: {result}")
                return []

            updates = result.get("result", [])

            if updates:
                self.last_update_id = updates[-1]["update_id"]

            return updates

        except requests.exceptions.ConnectTimeout:
            logger.warning("Telegram connect timeout")

        except requests.exceptions.ReadTimeout:
            logger.warning("Telegram read timeout")

        except requests.exceptions.ConnectionError as e:
            logger.warning(f"Telegram connection error: {e}")
            time.sleep(5)

        except requests.exceptions.HTTPError as e:
            logger.error(f"Telegram HTTP error: {e}")

        except requests.exceptions.RequestException as e:
            logger.error(f"Telegram request error: {e}")

        except ValueError:
            logger.error("Telegram returned invalid JSON")

        except Exception:
            logger.exception("Unexpected error while getting Telegram updates")

        return []



    def process_update(self, update: Dict) -> Optional[Dict]:
        if 'callback_query' in update:
            return self._handle_callback(update['callback_query'])
        if 'message' in update:
            return self._handle_message(update['message'])
        return None

    # ------------------------------------------------------------------
    # Callbacks (confirm / reject)
    # ------------------------------------------------------------------

    def _handle_callback(self, callback: Dict) -> Optional[Dict]:
        callback_id = callback['id']
        data = callback.get('data', '')
        message_id = callback['message'].get('message_id')

        self.answer_callback(callback_id)

        if data.startswith('confirm_') and message_id in self.pending_bets:
            bet = self.pending_bets.pop(message_id)
            self.edit_message(
                message_id,
                f"*BEVESTIGD*\n\n"
                f"{bet.participant1} vs {bet.participant2}\n"
                f"{bet.soft_bookmaker} @ {bet.soft_odds}\n"
                f"Inzet: {bet.stake_amount:.2f}"
            )
            return {'action': 'confirm', 'bet': bet}

        if data.startswith('reject_') and message_id in self.pending_bets:
            bet = self.pending_bets.pop(message_id)
            self.edit_message(
                message_id,
                f"*AFGEWEZEN*\n\n{bet.participant1} vs {bet.participant2}"
            )
            return {'action': 'reject'}

        return None

    # ------------------------------------------------------------------
    # Message / command handling
    # ------------------------------------------------------------------

    def _handle_message(self, message: Dict) -> Optional[Dict]:
        text = message.get('text', '').strip()
        chat_id = str(message.get('chat', {}).get('id', self.chat_id))

        # If there is an active manual-entry session for this chat, feed the answer
        if chat_id in self._manual_sessions and not text.startswith('/'):
            return self._manual_step(chat_id, text)

        if not text.startswith('/'):
            return None

        cmd = text.split()[0].lower()

        dispatch = {
            '/run':      lambda: {'action': 'run'},
            '/stop':     lambda: {'action': 'stop'},
            '/profit':   self._cmd_profit,
            '/keys':     self._cmd_keys,
            '/bankroll': self._cmd_bankroll,
            '/set':      lambda: {'action': 'set'},
            '/manueel':  lambda: self._cmd_manueel(chat_id),
            '/annuleer': lambda: self._cmd_annuleer(chat_id),
            '/help':     self._cmd_help,
        }

        handler = dispatch.get(cmd)
        if handler:
            if cmd == '/run':
                self.send_message("*Scanner GESTART*", chat_id=chat_id)
            elif cmd == '/stop':
                self.send_message("*Scanner GESTOPT*", chat_id=chat_id)
            return handler()
        return None

    # ------------------------------------------------------------------
    # Manual bet entry flow
    # ------------------------------------------------------------------

    def _cmd_manueel(self, chat_id: str) -> Dict:
        session = ManualBetSession()
        self._manual_sessions[chat_id] = session
        _, question = session.current_step
        self.send_message(
            f"*Manuele bet invoer*\n\nStap 1/{len(MANUAL_STEPS)}: {question}\n\n"
            f"_(Typ /annuleer om te stoppen)_",
            chat_id=chat_id
        )
        return {'action': 'manueel_start'}

    def _cmd_annuleer(self, chat_id: str) -> Dict:
        self._manual_sessions.pop(chat_id, None)
        self.send_message("*Invoer geannuleerd.*", chat_id=chat_id)
        return {'action': 'manueel_cancel'}

    def _manual_step(self, chat_id: str, answer: str) -> Optional[Dict]:
        session = self._manual_sessions[chat_id]
        session.record_answer(answer)

        if not session.is_complete:
            step_num = session.step_index + 1
            _, question = session.current_step
            self.send_message(
                f"Stap {step_num}/{len(MANUAL_STEPS)}: {question}",
                chat_id=chat_id
            )
            return {'action': 'manueel_step'}

        # All answers collected — build the bet
        bankroll = self.sheets.get_bankroll() if self.sheets else 500
        try:
            bet = session.to_value_bet(bankroll)
        except (ValueError, KeyError) as e:
            self.send_message(
                f"*Fout bij verwerking:* {e}\n\nStart opnieuw met /manueel",
                chat_id=chat_id
            )
            del self._manual_sessions[chat_id]
            return {'action': 'manueel_error'}

        del self._manual_sessions[chat_id]

        # Show summary with confirm/reject buttons
        odds_table = self._format_odds_table(
            bet.soft_bookmaker_odds, bet.soft_bookmaker, bet.sharp_odds
        )
        summary = (
            f"*Samenvatting manuele bet*\n\n"
            f"*{bet.participant1}* vs *{bet.participant2}*\n"
            f"Start: {bet.start_time}\n"
            f"Sport: {bet.sport}\n"
            f"Competitie: {bet.tournament_name} ({bet.category_name})\n\n"
            f"Markt: {bet.market} | Uitkomst: *{bet.outcome}*\n\n"
            f"*Odds overzicht:*\n{odds_table}\n\n"
            f"*EV: {bet.ev_percentage:.2f}%*\n"
            f"*Inzet: €{bet.stake_amount:.2f}*\n\n"
            f"(Kelly: {bet.kelly_fraction:.2%} van {bet.bankroll:.0f})\n\n"
            f"Bet opslaan?"
        )
        keyboard = {
            "inline_keyboard": [[
                {"text": "Opslaan", "callback_data": f"confirm_{bet.fixture_id}_{bet.soft_bookmaker}_manual"},
                {"text": "Annuleer", "callback_data": f"reject_{bet.fixture_id}"}
            ]]
        }
        msg_id = self.send_message(summary, chat_id=chat_id, keyboard=keyboard)
        if msg_id is not None:
            self.pending_bets[msg_id] = bet

        return {'action': 'manueel_complete', 'bet': bet}

    # ------------------------------------------------------------------
    # Command implementations
    # ------------------------------------------------------------------

    def _cmd_profit(self) -> Dict:
        if self.sheets:
            p = self.sheets.get_profit_loss()
            wr = (p['wins'] / max(1, p['wins'] + p['losses'])) * 100
            self.send_message(
                f"*Winst/Verlies*\n\n"
                f"Gewonnen: {p['wins']}\n"
                f"Verloren: {p['losses']}\n"
                f"Open: {p['pending']}\n"
                f"Win rate: {wr:.1f}%"
            )
        else:
            self.send_message("Google Sheets niet geconfigureerd")
        return {'action': 'profit'}

    def _cmd_keys(self) -> Dict:
        if self._scanner and hasattr(self._scanner, 'odds_client'):
            s = self._scanner.odds_client.get_key_status()
            msg = (
                f"*API Keys*\n\n"
                f"Totaal: {s['total_requests']}\n"
                f"Resterend: {s['total_remaining']}\n\n"
            )
            for i, k in enumerate(s['keys'], 1):
                msg += f"Key {i}: {k['usage']}/{k['limit']} ({k['remaining']} over)\n"
            self.send_message(msg)
        else:
            self.send_message("Scanner niet beschikbaar")
        return {'action': 'keys'}

    def _cmd_bankroll(self) -> Dict:
        if self.sheets:
            br = self.sheets.get_bankroll()
            self.send_message(f"*Bankroll*\n\n{br:.2f}")
        else:
            self.send_message("Google Sheets niet geconfigureerd")
        return {'action': 'bankroll'}

    def _cmd_help(self) -> Dict:
        self.send_message(
            "*Beschikbare commando's*\n\n"
            "/run - Scanner starten\n"
            "/stop - Scanner stoppen\n"
            "/manueel - Bet handmatig invoeren\n"
            "/annuleer - Manuele invoer annuleren\n"
            "/profit - Winst/verlies overzicht\n"
            "/keys - API key gebruik\n"
            "/bankroll - Bankroll weergeven\n"
            "/set - Settlements bijwerken\n"
            "/help - Dit overzicht"
        )
        return {'action': 'help'}


class ValueBetScanner:
    """Main scanner orchestrator"""

    def __init__(self, config: Dict):
        self.config = config
        self.is_scanning = False
        self.settlements = []

        api_keys = config.get('oddspapi_keys', [])
        if not api_keys:
            single = config.get('oddspapi_key', '')
            api_keys = [single] if single else []


        if not api_keys or not api_keys[0]:
            raise ValueError("No API keys configured")


        self.odds_client = OddsPapiClient(
            api_keys, self.settlements, config.get('requests_per_key', 250), 
        )
        logger.info(f"Initialized with {len(api_keys)} API key(s)")

        self.calculator = ValueBetCalculator(
            min_ev_threshold=config.get('min_ev_threshold', 2.0),
            kelly_fraction=config.get('kelly_fraction', 0.25)
        )
        self.calculator.set_odds_client(self.odds_client)

        ManualBetSession(min_ev_threshold=config.get('min_ev_thresold', 2.0),
                         kelly_fraction=config.get('kelly_fraction', 0.25))

        self.sheets = None
        if config.get('google_credentials_path') and config.get('google_spreadsheet_id'):
            self.sheets = GoogleSheetsManager(
                config['google_credentials_path'],
                config['google_spreadsheet_id']
            )
            self.sheets.ensure_template_sheet()

        self.telegram = None
        if config.get('telegram_bot_token') and config.get('telegram_chat_id'):
            self.telegram = TelegramBot(
                config['telegram_bot_token'],
                config['telegram_chat_id'],
                self.sheets
            )
            self.telegram.set_scanner(self)

        self.seen_bets: set = set()
        self.confirmed_bets: List[Dict] = []
        self.confirmed_bet_keys = {
            f"{b['fixture_id']}_{b['soft_bookmaker']}_{b['outcome_id']}"
            for b in self.confirmed_bets
}
        self._load_seen()

    def _load_seen(self):
        try:
            if Path('seen_bets.json').exists():
                self.seen_bets = set(json.load(open('seen_bets.json')))
            if Path('confirmed_bets.jsonl').exists():
                for line in open('confirmed_bets.jsonl'):
                    if line.strip():
                        self.confirmed_bets.append(json.loads(line))
        except Exception:
            pass

    def _save_seen(self):
        json.dump(list(self.seen_bets), open('seen_bets.json', 'w'))

    def _save_confirmed(self, bet: ValueBet):
        data = {
            'fixture_id': bet.fixture_id,
            'market_id': bet.market_id,
            'outcome_id': bet.outcome_id,
            'timestamp': bet.timestamp,
            'soft_bookmaker': bet.soft_bookmaker,
            'soft_odds': bet.soft_odds,
            'stake_amount': bet.stake_amount,
            'status': 'open'
        }

        self.confirmed_bets.append(data)
        with open('confirmed_bets.jsonl', 'a') as f:
            f.write(json.dumps(data) + '\n')

    def get_bankroll(self) -> float:
        if self.sheets:
            return self.sheets.get_bankroll()
        return float(self.config.get('bankroll', 500))

    def update_settlements(self) -> str:
        if not self.confirmed_bets:
            return "Geen bets om bij te werken"

        fixture_ids = [b['fixture_id'] for b in self.confirmed_bets
                       if not b['fixture_id'].startswith('manual_')]
        self.odds_client.get_settlements(fixture_ids)
        updated = wins = losses = 0

        for i in self.settlements:
            for bet in self.confirmed_bets:
                fid = bet['fixture_id']
                outcome_id = bet['outcome_id']
                market_id = bet['market_id']

                if fid == i['fixtureId'] and bet['status'] == 'open':
                    result = i.get("markets",{}).get(market_id, {}).get("outcomes", {}).get(outcome_id, {}).get("players", {}).get("0", {}).get("result", 'UNKNOWN')

                    status = result.upper()
                    pprint((status, fid, outcome_id))
                    if 'WIN' in status:
                        wins += 1
                    elif 'LOSE' in status:
                        losses += 1

                    if self.sheets:
                        succes = self.sheets.update_settlement(fid, status)

                        if succes:
                            if status != "UNKNOWN":
                                bet['status'] = "closed"

                            updated += 1

        return f"Bijgewerkt: {updated}\nGewonnen: {wins}\nVerloren: {losses}"
    

    def update_main_sheet_totals(self):
        """Sum A2:F6 from all MM-YYYY sheets."""
        return self.sheets.update_main_sheet_totals()


    def scan_once(self) -> List[ValueBet]:
        logger.info("Scanning...")
        value_bets = []
        bankroll = self.get_bankroll()

        status = self.odds_client.get_key_status()
        logger.info(f"API Status: {status['total_remaining']} requests remaining")

        sport_ids = self.config.get('sport_id', [])
    
        if not sport_ids:
            raise ValueError("No sport id's configured")
            
        for id in sport_ids:
            tournaments = self.odds_client.get_tournaments(id)
            if tournaments is None:
                logger.info("Stopping scanner due to unforseen problems")
                msg = "Kon data niet ophalen, probeer opnieuw met andere keys of roteer IP adress"
                self.is_scanning = False
                self.telegram.send_message(msg)
                return 

            active = [t for t in tournaments
                    if t.get('upcomingFixtures', 0) > 0 or t.get('futureFixtures', 0) > 0]

            for tournament in active[:self.config.get('max_tournaments', 10)]:
                if not self.is_scanning:
                    break
                
                fixtures = self.odds_client.get_fixtures(
                    tournament_id=tournament['tournamentId'],
                    sport_id=id,
                    days_ahead=self.config.get('days_ahead', 7)
                )
                if fixtures is None:
                    msg = "Kon data niet ophalen, probeer opnieuw met andere keys of roteer IP adress"
                    logger.info("Stopping scanner due to unforseen problems")
                    self.is_scanning = False
                    self.telegram.send_message(msg)
                    return 

                for fixture in fixtures:
                    if not self.is_scanning:
                        break

                    odds_data = self.odds_client.get_odds(fixture['fixtureId'])
                    if odds_data is None:
                        logger.info("Stopping scanner due to unforseen problems")
                        msg = "Kon data niet ophalen, probeer opnieuw met andere keys of roteer IP adress"
                        self.is_scanning = False
                        self.telegram.send_message(msg)
                        return 

                    if not odds_data.get('bookmakerOdds'):
                        continue

                    bets = self.calculator.analyze_fixture(fixture, odds_data, bankroll)
                    for bet in bets:
                        key = f"{bet.fixture_id}_{bet.soft_bookmaker}_{bet.outcome_id}"

                        if key not in self.confirmed_bet_keys:
                            value_bets.append(bet)

                        #if key not in self.seen_bets:
                            #value_bets.append(bet)
                            #self.seen_bets.add(key)

                    time.sleep(self.config.get('request_delay', 1))


            self._save_seen()
            logger.info(f"Found {len(value_bets)} value bets for sport ID {id}")
            self.is_scanning = True
            
            for bet in value_bets:
                if not self.is_scanning:
                    break
                if self.telegram:
                    self.telegram.send_value_bet_notification(bet)
                else:
                    self._log_bet(bet)
    

    def run_interactive(self):
        if not self.telegram:
            logger.error("Telegram not configured")
            return

        self.telegram.send_message(
            "*Value Bet Scanner Gestart*\n\nGebruik /run om de scanner te starten\n/help voor alle commando's\nGebruik /set om settlements bij te werken"
        )

        scan_thread = None
        while True:
            try:
                for update in self.telegram.get_updates():
                    result = self.telegram.process_update(update)

                    if result:
                        action = result.get('action')

                        if action == 'run' and not self.is_scanning:
                            #logger.info("Updateing settlements")
                            #self.telegram.send_message("Settlements bijwerken...")
                            #msg = self.update_settlements()
                            #self.telegram.send_message(f"*Settlements*\n\n{msg}")
                            self.is_scanning = True

                            scan_thread = threading.Thread(
                                target=self.scan_once, daemon=True
                            )
                            scan_thread.start()
                           
                        elif action == 'stop':
                            self.is_scanning = False


                        elif action == 'confirm':
                            bet = result.get('bet')
                            if bet:
                                self._log_bet(bet)

                        elif action == 'set':
                            msg = self.update_settlements()
                            msg_dashboard = self.update_main_sheet_totals()
                            self.telegram.send_message(f"*Settlements*\n\n{msg}")
                            self.telegram.send_message(msg_dashboard)

                time.sleep(1)
            except KeyboardInterrupt:
                self.is_scanning = False
                break
            except Exception:
                logger.exception(f"Loop error")
                time.sleep(5)

    def _log_bet(self, bet: ValueBet):
        """Write a confirmed bet to the monthly Google Sheet."""
        if self.sheets:
            d = bet.to_dict()
            row = [d.get(h, '') for h in SHEET_HEADERS]
            sheet_name = self.sheets.get_or_create_monthly_sheet()
            self.sheets.append_row(row, sheet_name=sheet_name)
        self._save_confirmed(bet)
        logger.info(f"Bet opgeslagen: {bet.fixture_id}")

    def run_single(self):
        bets = self.scan_once()
        logger.info(f"\n{'='*50}\nGEVONDEN {len(bets)} VALUE BETS\n{'='*50}")
        for bet in bets:
            logger.info(
                f"\n{bet.participant1} vs {bet.participant2}\n"
                f"{bet.soft_bookmaker} @ {bet.soft_odds}\n"
                f"EV: {bet.ev_percentage:.2f}%\n"
                f"Inzet: {bet.stake_amount:.2f}\n"
                f"Betslip: {bet.betslip_url}"
            )
            self._log_bet(bet)


def load_config(path: str = 'config.json') -> Dict:
    config = {}

    if Path(path).exists():
        config = json.load(open(path))
   
    
    else:
        keys_str = os.getenv('ODDSPAPI_KEYS', os.getenv('ODDSPAPI_KEY', ''))
        if keys_str:
            config['oddspapi_keys'] = [k.strip() for k in keys_str.split(',') if k.strip()]

        config.setdefault('telegram_bot_token', os.getenv('TELEGRAM_BOT_TOKEN', ''))
        config.setdefault('telegram_chat_id', os.getenv('TELEGRAM_CHAT_ID', ''))
        config.setdefault('google_credentials_path', os.getenv('GOOGLE_CREDENTIALS_PATH', ''))
        config.setdefault('google_spreadsheet_id', os.getenv('GOOGLE_SPREADSHEET_ID', ''))
        config.setdefault('min_ev_threshold', float(os.getenv('MIN_EV_THRESHOLD', '2.0')))
        config.setdefault('kelly_fraction', float(os.getenv('KELLY_FRACTION', '0.25')))
        config.setdefault('bankroll', float(os.getenv('BANKROLL', '1000')))
        config.setdefault('sport_id', int(os.getenv('SPORT_ID', '10')))
        config.setdefault('max_tournaments', int(os.getenv('MAX_TOURNAMENTS', '10')))
        config.setdefault('days_ahead', int(os.getenv('DAYS_AHEAD', '7')))
        config.setdefault('request_delay', float(os.getenv('REQUEST_DELAY', '1')))
        config.setdefault('scan_interval', int(os.getenv('SCAN_INTERVAL', '300')))
        config.setdefault('requests_per_key', int(os.getenv('REQUESTS_PER_KEY', '250')))

    return config


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Value Bet Scanner')
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--interactive', action='store_true',
                        help='Telegram interactive mode')
    parser.add_argument('--sport', type=int, nargs='+', default=[10, 11, 13], help="Sport ID's")
    parser.add_argument('--ev', type=float, default=10)

    args = parser.parse_args()
    config = load_config(args.config)
    config['sport_id'] = args.sport

    if args.sport is not None:
        if isinstance(args.sport, list):
            config["sport_id"] = args.sport
        else:
            config["sport_id"] = [args.sport]


    config['min_ev_threshold'] = args.ev

    if not config.get('oddspapi_keys'):
        logger.error("API keys vereist")
        return

    scanner = ValueBetScanner(config)

    if args.interactive:
        scanner.run_interactive()
    else:
        scanner.run_single()


if __name__ == '__main__':
    main()
