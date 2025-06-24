from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from google.oauth2 import service_account
import io
import os

# Configuration
CREDENTIALS_FILE = 'credentials.json'
SCOPES = ['https://www.googleapis.com/auth/drive']
TSV_FILENAME = 'boardgames.tsv'
DRIVE_FILE_ID = os.getenv("DRIVE_TSV_FILE_ID")  # ID of file in Google Drive

def get_drive_service():
    creds = service_account.Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    return build('drive', 'v3', credentials=creds)

def download_tsv_from_gdrive():
    """Download TSV file from Google Drive"""
    service = get_drive_service()
    request = service.files().get_media(fileId=DRIVE_FILE_ID)
    fh = io.FileIO(TSV_FILENAME, 'wb')
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()

def upload_tsv_to_gdrive():
    """Upload TSV file to Google Drive (overwrite)"""
    service = get_drive_service()
    media = MediaIoBaseUpload(io.FileIO(TSV_FILENAME, 'rb'), mimetype='text/tab-separated-values')
    service.files().update(
        fileId=DRIVE_FILE_ID,
        media_body=media
    ).execute()
