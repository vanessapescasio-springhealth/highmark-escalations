"""
Highmark Escalation Automation
Fetches Highmark emails from Gmail, redacts PHI using Claude, creates Asana tasks
"""

import os
import base64
import json
from datetime import datetime
from google.auth.transport.requests import Request
from google.oauth2.service_account import Credentials
from google.oauth2 import service_account
import google.auth
from google.auth.oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from anthropic import Anthropic
import requests

# Initialize clients
gmail_service = None
asana_client = None
claude_client = Anthropic()

def setup_gmail():
    """Setup Gmail API client using service account or OAuth"""
    global gmail_service
    
    # Try to use service account first (for GitHub Actions)
    service_account_json = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON')
    if service_account_json:
        try:
            service_account_info = json.loads(service_account_json)
            credentials = service_account.Credentials.from_service_account_info(
                service_account_info,
                scopes=['https://www.googleapis.com/auth/gmail.readonly']
            )
            gmail_service = build('gmail', 'v1', credentials=credentials)
            return gmail_service
        except Exception as e:
            print(f"Service account setup failed: {e}")
    
    # Fallback to OAuth for local testing
    SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']
    credentials = None
    
    if os.path.exists('token.json'):
        credentials = Credentials.from_authorized_user_file('token.json', SCOPES)
    
    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'credentials.json', SCOPES)
            credentials = flow.run_local_server(port=0)
        
        with open('token.json', 'w') as token:
            token.write(credentials.to_json())
    
    gmail_service = build('gmail', 'v1', credentials=credentials)
    return gmail_service

def fetch_highmark_emails():
    """Fetch unread emails from HMKEscalations that contain [secure] in subject"""
    try:
        results = gmail_service.users().messages().list(
            userId='me',
            q='from:HMKEscalations@springhealth.com subject:[secure] is:unread',
            maxResults=10
        ).execute()
        
        messages = results.get('messages', [])
        return messages
    except Exception as e:
        print(f"Error fetching emails: {e}")
        return []

def is_initial_email(message_id):
    """Check if email is initial (not a reply in thread)"""
    try:
        msg = gmail_service.users().messages().get(
            userId='me',
            id=message_id,
            format='full'
        ).execute()
        
        headers = msg['payload']['headers']
        subject = next((h['value'] for h in headers if h['name'] == 'Subject'), '')
        in_reply_to = next((h['value'] for h in headers if h['name'] == 'In-Reply-To'), None)
        references = next((h['value'] for h in headers if h['name'] == 'References'), None)
        
        # If no In-Reply-To or References, it's an initial email
        return in_reply_to is None and references is None
    except Exception as e:
        print(f"Error checking email thread status: {e}")
        return True  # Assume initial if we can't determine

def get_email_content(message_id):
    """Extract email subject, sender, and date"""
    try:
        msg = gmail_service.users().messages().get(
            userId='me',
            id=message_id,
            format='full'
        ).execute()
        
        headers = msg['payload']['headers']
        subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject')
        sender = next((h['value'] for h in headers if h['name'] == 'From'), 'Unknown')
        date = next((h['value'] for h in headers if h['name'] == 'Date'), 'Unknown date')
        
        return {
            'subject': subject,
            'sender': sender,
            'date': date,
            'message_id': message_id
        }
    except Exception as e:
        print(f"Error extracting email content: {e}")
        return None



def create_asana_task(subject, sender, date, message_id):
    """Create a task in Asana with encrypted email details"""
    try:
        asana_token = os.getenv('ASANA_API_TOKEN')
        asana_project_id = os.getenv('ASANA_PROJECT_ID')
        
        if not asana_token or not asana_project_id:
            print("Missing Asana credentials in environment variables")
            return False
        
        headers = {
            'Authorization': f'Bearer {asana_token}',
            'Content-Type': 'application/json'
        }
        
        # Task description notes that email is encrypted and needs manual review
        # IMPORTANT: Gmail Message ID is stored here so we can match replies to this task
        task_notes = f"""🔒 ENCRYPTED EMAIL - Outlook Message Encryption (OME)

From: {sender}
Date: {date}
Gmail Message ID: {message_id}

⚠️ ACTION REQUIRED:
1. Open your Gmail inbox
2. Find the original email from HMKEscalations@springhealth.com
3. Click the link to decrypt the message (Outlook will send a one-time passcode)
4. Copy the decrypted email body below and add any additional context needed

[PASTE DECRYPTED EMAIL BODY HERE]

---
[System: This task tracks escalation ID {message_id}]"""
        
        task_data = {
            'data': {
                'name': f'[Highmark] {subject}',
                'notes': task_notes,
                'projects': [asana_project_id]
            }
        }
        
        response = requests.post(
            'https://www.asana.com/api/1.0/tasks',
            headers=headers,
            json=task_data
        )
        
        if response.status_code == 201:
            task_id = response.json()['data']['gid']
            print(f"✅ Created Asana task {task_id} for: {subject}")
            return task_id
        else:
            print(f"❌ Failed to create Asana task: {response.status_code} - {response.text}")
            return None
    except Exception as e:
        print(f"Error creating Asana task: {e}")
        return None

def mark_email_as_read(message_id):
    """Mark email as read to prevent reprocessing"""
    try:
        gmail_service.users().messages().modify(
            userId='me',
            id=message_id,
            body={'removeLabelIds': ['UNREAD']}
        ).execute()
    except Exception as e:
        print(f"Error marking email as read: {e}")

def find_asana_task_by_gmail_id(gmail_message_id):
    """Search Asana for a task containing the Gmail message ID in its description"""
    try:
        asana_token = os.getenv('ASANA_API_TOKEN')
        asana_project_id = os.getenv('ASANA_PROJECT_ID')
        
        if not asana_token or not asana_project_id:
            return None
        
        headers = {
            'Authorization': f'Bearer {asana_token}',
            'Content-Type': 'application/json'
        }
        
        # Query tasks in the project
        response = requests.get(
            f'https://www.asana.com/api/1.0/projects/{asana_project_id}/tasks?opt_fields=gid,name,notes',
            headers=headers
        )
        
        if response.status_code == 200:
            tasks = response.json()['data']
            # Find task with matching Gmail message ID in notes
            for task in tasks:
                if task.get('notes') and gmail_message_id in task['notes']:
                    return task['gid']
        
        return None
    except Exception as e:
        print(f"Error finding Asana task: {e}")
        return None

def add_comment_to_asana_task(task_id, comment_text):
    """Add a comment to an Asana task"""
    try:
        asana_token = os.getenv('ASANA_API_TOKEN')
        
        if not asana_token:
            return False
        
        headers = {
            'Authorization': f'Bearer {asana_token}',
            'Content-Type': 'application/json'
        }
        
        comment_data = {
            'data': {
                'text': comment_text
            }
        }
        
        response = requests.post(
            f'https://www.asana.com/api/1.0/tasks/{task_id}/stories',
            headers=headers,
            json=comment_data
        )
        
        if response.status_code == 201:
            print(f"   ✅ Added comment to task {task_id}")
            return True
        else:
            print(f"   ❌ Failed to add comment: {response.status_code}")
            return False
    except Exception as e:
        print(f"Error adding comment to Asana task: {e}")
        return False

def get_thread_root_message_id(message_id):
    """Get the root/initial message ID in a thread"""
    try:
        msg = gmail_service.users().messages().get(
            userId='me',
            id=message_id,
            format='full'
        ).execute()
        
        headers = msg['payload']['headers']
        references = next((h['value'] for h in headers if h['name'] == 'References'), '')
        
        if references:
            # Get the first message ID from References (the root of the thread)
            root_id = references.split()[0].strip('<>')
            return root_id
        
        # If no references, this is likely the root
        return message_id
    except Exception as e:
        print(f"Error getting thread root: {e}")
        return None

def main():
    """Main orchestration function"""
    print(f"\n🚀 Starting Highmark escalation sync at {datetime.now()}")
    
    # Setup Gmail client
    setup_gmail()
    if not gmail_service:
        print("Failed to setup Gmail client")
        return
    
    # Fetch unread Highmark emails with [secure] in subject
    emails = fetch_highmark_emails()
    print(f"Found {len(emails)} unread Highmark secure escalations")
    
    if not emails:
        print("No new escalations to process")
        return
    
    # Process each email
    for message in emails:
        message_id = message['id']
        
        # Check if it's an initial email (not a reply)
        if is_initial_email(message_id):
            # INITIAL EMAIL - Create new Asana task
            email_data = get_email_content(message_id)
            if not email_data:
                continue
            
            print(f"\n📧 Processing initial escalation: {email_data['subject']}")
            
            # Create Asana task with encryption notice
            print("   📝 Creating Asana task...")
            task_id = create_asana_task(
                email_data['subject'],
                email_data['sender'],
                email_data['date'],
                message_id
            )
            
            # Mark as read only if Asana task was created
            if task_id:
                mark_email_as_read(message_id)
        
        else:
            # REPLY EMAIL - Add as comment to existing task
            print(f"\n💬 Processing reply (ID: {message_id})")
            
            # Get the root message ID of this thread
            root_message_id = get_thread_root_message_id(message_id)
            if not root_message_id:
                print("   ⚠️  Could not determine thread root")
                continue
            
            # Find the Asana task for this thread
            task_id = find_asana_task_by_gmail_id(root_message_id)
            if not task_id:
                print(f"   ⚠️  No Asana task found for thread (root: {root_message_id})")
                continue
            
            # Extract reply content
            email_data = get_email_content(message_id)
            if not email_data:
                continue
            
            # Format comment with sender and date
            comment_text = f"""📧 Reply from: {email_data['sender']}
Date: {email_data['date']}

⚠️ ENCRYPTED - Decrypt in Outlook and paste content here:
[PASTE DECRYPTED REPLY HERE]"""
            
            # Add comment to task
            print(f"   📝 Adding comment to task {task_id}...")
            if add_comment_to_asana_task(task_id, comment_text):
                mark_email_as_read(message_id)
    
    print(f"\n✨ Sync completed at {datetime.now()}\n")

if __name__ == '__main__':
    main()
