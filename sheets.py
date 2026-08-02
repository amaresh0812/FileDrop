import os,json
from dotenv import load_dotenv
from flask import Flask,request
import logging
import random
import socket
import requests
from datetime import datetime, timezone
import gspread
from gspread.exceptions import (
    APIError,
    SpreadsheetNotFound,
    WorksheetNotFound,
)
import time
from google.oauth2.service_account import Credentials
load_dotenv()
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

SCOPES = [
 "https://www.googleapis.com/auth/spreadsheets",
 "https://www.googleapis.com/auth/drive.readonly",
]
class SheetService:
    def __init__(self):
        
        self.creds = Credentials.from_service_account_info(
 json.loads(os.environ.get("GSPREAD_CREDENTIALS")),
 scopes=SCOPES,
)   
        self.CONTACT_KEY = os.environ.get("CONTACT_KEY")
        
        self.gc = gspread.authorize(self.creds)
        # open the spreadsheet by key
        try:
            self.sheet = self.gc.open_by_key(self.CONTACT_KEY)
        except Exception as e:
            log.error("Failed to open spreadsheet with key %s: %s", self.CONTACT_KEY, e)
            self.sheet = None

    def open_sheet(self, name):

        try:
            
            return self.sheet.worksheet(name)
        except Exception as e:
            log.error("Failed to open Google Sheet with key %s: %s", name, e)
            return None
    
#contact form append row
    def contact_append_row(self,name,email,message,ip_address):

        ws = self.open_sheet("ContactForm")

        if ws is None:
            return False

        row = [
            datetime.utcnow().strftime("%H:%M:%S %d-%m-%Y"),
            name,
            email,
            message,
            ip_address,
        ]

        self.execute_with_retry(
            ws.append_row,
            row,
            value_input_option="USER_ENTERED",
        )

        log.info("Contact saved for %s", email)

        return True

    def feedback_append_row(self, name, email, message, ip_address):
        ws = self.open_sheet("FeedbackForm")
        if ws is None:
            return False
        row = [
            datetime.utcnow().strftime("%H:%M:%S %d-%m-%Y"),
            name,
            email,
            message,
            ip_address,
        ]
        self.execute_with_retry(
            ws.append_row,
            row,
            value_input_option="USER_ENTERED",
        )
        log.info("Feedback saved for %s", email)
        return True
    

    def execute_with_retry(self,func, *args, **kwargs):
        max_retries = 3
        timeout = 5  # seconds
        for attempt in range(max_retries):
            try:
                return func(*args, **kwargs)

            except APIError as e:

                status = getattr(e.response, "status_code", None)

                if status == 429:
                    wait = (2 ** attempt) + random.uniform(0, 1)
                    log.warning(
                        "Rate limit exceeded. Retrying in %.2f seconds.",
                        wait,
                    )
                    time.sleep(wait)
                    continue

                elif status and status >= 500:
                    wait = (2 ** attempt) + random.uniform(0, 1)
                    log.warning(
                        "Google server error %s. Retry in %.2f sec.",
                        status,
                        wait,
                    )
                    time.sleep(wait)
                    continue

                raise

            except (
                requests.ConnectionError,
                requests.Timeout,
                socket.timeout,
            ):

                wait = (2 ** attempt) + random.uniform(0, 1)

                log.warning(
                    "Network error. Retry in %.2f sec.",
                    wait,
                )

                time.sleep(wait)

            except SpreadsheetNotFound:
                log.error("Spreadsheet not found.")
                raise

            except WorksheetNotFound:
                log.error("Worksheet not found.")
                raise

            except Exception:
                log.exception("Unexpected error.")
                raise

        raise RuntimeError(
            "Maximum retries exceeded while writing to Google Sheets."
        )

        
        
                    
                
