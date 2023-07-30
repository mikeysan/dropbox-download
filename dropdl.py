import os
import time
import dropbox
from dropbox.oauth import DropboxOAuth2FlowNoRedirect
from dotenv import load_dotenv

# Adding imports relating to dropbox download service.
import sqlite3
import logging
from concurrent.futures import ThreadPoolExecutor
from dropbox.exceptions import AuthError, RateLimitError
from collections import deque

# Load environment variables from .env file
load_dotenv()

class DropboxAuth:
    # Initialize with app key, app secret, and optionally access and refresh tokens
    def __init__(self, app_key, app_secret, token_access=None, token_refresh=None):
        self.app_key = app_key
        self.app_secret = app_secret
        self.token_access = token_access
        self.token_refresh = token_refresh
        self.dbx = None  # This will hold our Dropbox client once we're authenticated

    # This function handles the authentication process
    def authenticate(self):
        try:
            # If we have access and refresh tokens, we try to authenticate with them
            if self.token_access and self.token_refresh:
                self.dbx = dropbox.Dropbox(
                    oauth2_access_token=self.token_access,
                    oauth2_refresh_token=self.token_refresh,
                    app_key=self.app_key,
                    app_secret=self.app_secret
                )
                # This will refresh the access token if it's expired
                self.dbx.check_and_refresh_access_token()
            else:
                # If we don't have tokens, we start the OAuth flow to get them
                self.start_oauth_flow()
        except dropbox.exceptions.AuthError:
            # If the access token was invalid, we also start the OAuth flow
            print("Access token was invalid. Starting OAuth flow...")
            self.start_oauth_flow()

    # This function starts the OAuth flow
    def start_oauth_flow(self):
        auth_flow = DropboxOAuth2FlowNoRedirect(self.app_key, self.app_secret, token_access_type='offline')
        authorize_url = auth_flow.start()
        print("1. Go to: " + authorize_url)
        print("2. Click \"Allow\" (you might have to log in first).")
        print("3. Copy the authorization code.")
        auth_code = input("Enter the authorization code here: ").strip()
        oauth_result = auth_flow.finish(auth_code)
        # We store the access and refresh tokens for future use
        self.token_access = oauth_result.access_token
        self.token_refresh = oauth_result.refresh_token
        # And we create the Dropbox client with the new tokens
        self.dbx = dropbox.Dropbox(oauth2_access_token=self.token_access, oauth2_refresh_token=self.token_refresh, app_key=self.app_key, app_secret=self.app_secret)

    # This function returns a Dropbox client, authenticating if necessary
    def get_client(self):
        if not self.dbx:
            self.authenticate()
        return self.dbx


# Provide the `app_key` and `app_secret` stored externally in a .env file.
app_key = os.getenv('APP_KEY')
app_secret = os.getenv('APP_SECRET')

# If you already have the access and refresh tokens, you can provide them here
token_access = 'your-access-token'  # Optional
token_refresh = 'your-refresh-token'  # Optional

dbx_auth = DropboxAuth(app_key, app_secret, token_access, token_refresh)
# Now create a client instance. 
dbx = dbx_auth.get_client()
# Authorization code received: dCz-nfkKmeQAAAAAAAC_w8tC7Mt-IlXYtM5yvjZFHgA


# Main purpose of script starts here:

# Set up logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(levelname)s: %(message)s')




def is_file_downloaded(path, conn):
    """Check if a file has already been downloaded."""
    c = conn.cursor()
    c.execute("SELECT status FROM downloads WHERE path = ?", (path,))
    row = c.fetchone()
    return row is not None and row[0] == 'complete'


def download_file(path, local_path, conn):
    """Download a single file."""
    try:
        metadata, res = dbx.files_download(path)
        with open(local_path, 'wb') as f:
            f.write(res.content)
        logging.info(f"Downloaded {path} to {local_path}")
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO downloads VALUES (?, 'complete')", (path,))
        conn.commit()
    except RateLimitError as e:
        retry_after = e.error.retry_after if e.error.is_path() and e.error.get_path().is_conflict() else 5
        logging.warning(f"Rate limit error, sleeping for {retry_after} seconds before retrying...")
        time.sleep(retry_after)
        download_file(path, local_path, conn)
    except AuthError as e:
        logging.error(f"Authentication error: {e}")
        raise e  # Raise the exception to stop the script

def traverse_folder(conn, path=""):
    """Traverse a folder and download all files."""
    # Get a file path from the user, using forward slashes as the separator
    user_path = input("Enter a file path (use / as the separator): ")
    # 
    # Convert to the appropriate format for the current platform
    platform_path = os.path.normpath(user_path)
    # print(f"The platform-specific path is: {platform_path}")
    # Create a queue and add the root directory
    queue = deque([path])

    while queue:
        # Get the next directory from the queue
        path = queue.popleft()

        try:
            # List all files in the current directory
            result = dbx.files_list_folder(path)

            # Download each file and add each subdirectory to the queue
            with ThreadPoolExecutor(max_workers=5) as executor:
                for entry in result.entries:
                    if isinstance(entry, dropbox.files.FileMetadata):
                        # Old and working method. Uncomment below if needed.
                        # local_path = os.path.join('D:\\Dropbox\\downloads', entry.path_display[1:])
                        # New method. using user input to determine download location.
                        local_path = os.path.join(platform_path, entry.path_display[1:])
                        os.makedirs(os.path.dirname(local_path), exist_ok=True)
                        if not is_file_downloaded(entry.path_lower, conn):
                            try:
                                executor.submit(download_file, entry.path_lower, local_path, conn)
                            except dropbox.exceptions.AuthError as e:
                                logging.error(f"Authentication error: {e}")
                                return
                    elif isinstance(entry, dropbox.files.FolderMetadata):
                        queue.append(entry.path_lower)
        except RateLimitError as e:
            retry_after = e.error.retry_after if e.error.is_path() and e.error.get_path().is_conflict() else 5
            logging.warning(f"Rate limit error, sleeping for {retry_after} seconds before retrying...")
            time.sleep(retry_after)
            queue.append(path)
        except AuthError as e:
            logging.error(f"Authentication error: {e}")
        except ConnectionError:
            print("Connection error occurred. Retrying after 5 seconds...")
            time.sleep(10)
            queue.append(path)
        except dropbox.exceptions.ApiError as e:
            if isinstance(e.error.get_path(), dropbox.files.LookupError) and e.error.get_path().is_locked():
                logging.warning(f"Folder is locked, sleeping for 60 seconds before retrying...")
                time.sleep(60)
                queue.append(path)
            else:
                logging.error(f"API error: {e}")


def main():
    """Main function."""
    conn = sqlite3.connect('downloads.db')
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS downloads (
            path TEXT PRIMARY KEY,
            status TEXT
        )
    """)
    conn.commit()
    traverse_folder(conn, '/')  # TODO: Replace hardcoded path with run argument or user input.
    
    conn.close()


if __name__ == "__main__":
    main()
