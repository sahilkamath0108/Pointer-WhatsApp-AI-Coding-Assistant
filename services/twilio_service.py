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

    def download_media(self, url: str) -> tuple[bytes, str]:
        """
        Fetch media from Twilio's MediaUrl (HTTP Basic auth with Account SID / Auth Token).
        Returns (bytes, mime_type).
        """
        if not self.account_sid or not self.auth_token:
            raise ValueError("Twilio credentials required to download media")
        logger.info("Downloading media from Twilio URL")
        r = requests.get(
            url,
            auth=(self.account_sid, self.auth_token),
            timeout=45,
        )
        r.raise_for_status()
        ct = (r.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
        return r.content, ct