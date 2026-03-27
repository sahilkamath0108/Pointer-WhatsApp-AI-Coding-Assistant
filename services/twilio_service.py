from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
import os
import requests
from utils.logger import logger

class TwilioService:
    def __init__(self):
        self.account_sid = os.environ.get('TWILIO_ACCOUNT_SID')
        self.auth_token = os.environ.get('TWILIO_AUTH_TOKEN')
        self.phone_number = os.environ.get('TWILIO_PHONE_NUMBER')
        
        if not self.account_sid or not self.auth_token:
            logger.warning("Twilio credentials not found in environment variables")
        
        self.client = Client(self.account_sid, self.auth_token) if self.account_sid and self.auth_token else None
        
        if self.client:
            logger.info("Twilio service initialized successfully")
        
    def create_response(self, message):
        """
        Create a TwiML response for WhatsApp
        """
        logger.info(f"Creating TwiML response with {len(message)} chars")
        resp = MessagingResponse()
        resp.message(message)
        return str(resp)
    
    def send_message(self, to, message):
        """
        Send a WhatsApp message via REST API (for follow-ups after a fast webhook ack).
        """
        if not self.client:
            logger.error("Twilio client not initialized. Check your environment variables.")
            raise ValueError("Twilio client not initialized. Check your environment variables.")
            
        logger.info(f"Sending WhatsApp message to {to}")
        
        try:
            msg = self.client.messages.create(
                from_=self.phone_number,
                body=message,
                to=to
            )
            
            logger.info(f"Message sent successfully, SID: {msg.sid}")
            return msg.sid
        except Exception as e:
            logger.error(f"Failed to send message: {str(e)}")
            raise

    def download_media(
        self,
        url: str,
        content_type_hint: str | None = None,
    ) -> tuple[bytes, str]:
        """
        Fetch media from Twilio's MediaUrl{N} (see Twilio webhook docs / blog).

        Try HTTP Basic auth (Account SID + Auth Token) first — required for many api.twilio.com
        media URLs. If that fails with 401/403, retry a plain GET like the Twilio blog example.
        Use MediaContentType{N} from the webhook as hint when the response has no useful type.
        """
        if not url or not url.strip():
            raise ValueError("Empty media URL")

        def _normalize_ct(h: str | None) -> str:
            if not h:
                return "image/jpeg"
            return h.split(";")[0].strip()

        def _bytes_and_mime(resp: requests.Response) -> tuple[bytes, str]:
            header_ct = resp.headers.get("Content-Type")
            if not header_ct or "octet-stream" in header_ct.lower():
                ct = _normalize_ct(content_type_hint or header_ct)
            else:
                ct = _normalize_ct(header_ct)
            return resp.content, ct

        clean_url = url.strip()
        logger.info("Downloading media from Twilio URL")

        if self.account_sid and self.auth_token:
            r = requests.get(
                clean_url,
                auth=(self.account_sid, self.auth_token),
                timeout=45,
            )
            if r.ok:
                return _bytes_and_mime(r)
            if r.status_code in (401, 403):
                logger.warning(
                    "Authenticated media GET returned %s; retrying without auth (Twilio blog pattern)",
                    r.status_code,
                )
            else:
                r.raise_for_status()

        r = requests.get(clean_url, timeout=45)
        r.raise_for_status()
        return _bytes_and_mime(r)