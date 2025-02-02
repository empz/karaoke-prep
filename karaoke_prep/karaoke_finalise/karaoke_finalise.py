import os
import sys
import json
import shlex
import logging
import zipfile
import shutil
import re
import requests
import pickle
from thefuzz import fuzz
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.http import MediaFileUpload


class KaraokeFinalise:
    def __init__(
        self,
        log_level=logging.DEBUG,
        log_formatter=None,
        dry_run=False,
        model_name=None,
        instrumental_format="flac",
        brand_prefix=None,
        organised_dir=None,
        public_share_dir=None,
        youtube_client_secrets_file=None,
        youtube_description_file=None,
        rclone_destination=None,
        discord_webhook_url=None,
    ):
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(log_level)
        self.log_level = log_level
        self.log_formatter = log_formatter

        self.log_handler = logging.StreamHandler()

        if self.log_formatter is None:
            self.log_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(module)s - %(message)s")

        self.log_handler.setFormatter(self.log_formatter)
        self.logger.addHandler(self.log_handler)

        self.logger.debug(
            f"KaraokeFinalise instantiating, dry_run: {dry_run}, model_name: {model_name}, brand_prefix: {brand_prefix}, organised_dir: {organised_dir}, public_share_dir: {public_share_dir}, rclone_destination: {rclone_destination}"
        )

        # Path to the Windows PyInstaller frozen bundled ffmpeg.exe, or the system-installed FFmpeg binary on Mac/Linux
        ffmpeg_path = os.path.join(sys._MEIPASS, "ffmpeg.exe") if getattr(sys, "frozen", False) else "ffmpeg"

        self.ffmpeg_base_command = f"{ffmpeg_path} -hide_banner -nostats"

        if self.log_level == logging.DEBUG:
            self.ffmpeg_base_command += " -loglevel verbose"
        else:
            self.ffmpeg_base_command += " -loglevel fatal"

        self.dry_run = dry_run
        self.model_name = model_name
        self.instrumental_format = instrumental_format

        self.brand_prefix = brand_prefix
        self.organised_dir = organised_dir
        self.public_share_dir = public_share_dir
        self.youtube_client_secrets_file = youtube_client_secrets_file
        self.youtube_description_file = youtube_description_file
        self.rclone_destination = rclone_destination
        self.discord_webhook_url = discord_webhook_url

        self.youtube_upload_enabled = False
        self.discord_notication_enabled = False
        self.folder_organisation_enabled = False
        self.public_share_copy_enabled = False
        self.public_share_rclone_enabled = False

        self.skip_notifications = False

        self.suffixes = {
            "title_mov": " (Title).mov",
            "title_jpg": " (Title).jpg",
            "with_vocals_mov": " (With Vocals).mov",
            "karaoke_cdg": " (Karaoke).cdg",
            "karaoke_mp3": " (Karaoke).mp3",
            "karaoke_mov": " (Karaoke).mov",
            "final_karaoke_mp4": " (Final Karaoke).mp4",
            "final_karaoke_zip": " (Final Karaoke).zip",
        }

        self.youtube_url_prefix = "https://www.youtube.com/watch?v="

        self.youtube_url = None
        self.brand_code = None
        self.new_brand_code_dir_path = None

    def check_input_files_exist(self, base_name, with_vocals_file, instrumental_audio_file):
        self.logger.info(f"Checking required input files exist...")

        input_files = {
            "title_mov": f"{base_name}{self.suffixes['title_mov']}",
            "title_jpg": f"{base_name}{self.suffixes['title_jpg']}",
            "karaoke_cdg": f"{base_name}{self.suffixes['karaoke_cdg']}",
            "karaoke_mp3": f"{base_name}{self.suffixes['karaoke_mp3']}",
            "instrumental_audio": instrumental_audio_file,
            "with_vocals_mov": with_vocals_file,
        }

        for key, file_path in input_files.items():
            if not os.path.isfile(file_path):
                raise Exception(f"Input file {key} not found: {file_path}")

            self.logger.info(f" Input file {key} found: {file_path}")

        return input_files

    def prepare_output_filenames(self, base_name):
        return {
            "karaoke_mov": f"{base_name}{self.suffixes['karaoke_mov']}",
            "final_karaoke_mp4": f"{base_name}{self.suffixes['final_karaoke_mp4']}",
            "final_karaoke_zip": f"{base_name}{self.suffixes['final_karaoke_zip']}",
        }

    def prompt_user_confirmation_or_raise_exception(self, prompt_message, exit_message, allow_empty=False):
        if not self.prompt_user_bool(prompt_message, allow_empty=allow_empty):
            self.logger.error(exit_message)
            raise Exception(exit_message)

    def prompt_user_bool(self, prompt_message, allow_empty=False):
        options_string = "[y]/n" if allow_empty else "y/[n]"
        accept_responses = ["y", "yes"]
        if allow_empty:
            accept_responses.append("")

        print()
        response = input(f"{prompt_message} {options_string} ").strip().lower()
        return response in accept_responses

    def validate_input_parameters_for_features(self):
        self.logger.info(f"Validating input parameters for enabled features...")

        current_directory = os.getcwd()
        self.logger.info(f"Current directory to process: {current_directory}")

        # Enable youtube upload if client secrets file is provided and is valid JSON
        if self.youtube_client_secrets_file is not None and self.youtube_description_file is not None:
            if not os.path.isfile(self.youtube_client_secrets_file):
                raise Exception(f"YouTube client secrets file does not exist: {self.youtube_client_secrets_file}")

            if not os.path.isfile(self.youtube_description_file):
                raise Exception(f"YouTube description file does not exist: {self.youtube_description_file}")

            # Test parsing the file as JSON to check it's valid
            try:
                with open(self.youtube_client_secrets_file, "r") as f:
                    json.load(f)
            except json.JSONDecodeError as e:
                raise Exception(f"YouTube client secrets file is not valid JSON: {self.youtube_client_secrets_file}") from e

            self.logger.debug(f"YouTube upload checks passed, enabling YouTube upload")
            self.youtube_upload_enabled = True

        # Enable discord notifications if webhook URL is provided and is valid URL
        if self.discord_webhook_url is not None:
            if not self.discord_webhook_url.startswith("https://discord.com/api/webhooks/"):
                raise Exception(f"Discord webhook URL is not valid: {self.discord_webhook_url}")

            self.logger.debug(f"Discord webhook URL checks passed, enabling Discord notifications")
            self.discord_notication_enabled = True

        # Enable folder organisation if brand prefix and target directory are provided and target directory is valid
        if self.brand_prefix is not None and self.organised_dir is not None:
            if not os.path.isdir(self.organised_dir):
                raise Exception(f"Target directory does not exist: {self.organised_dir}")

            self.logger.debug(f"Brand prefix and target directory provided, enabling folder organisation")
            self.folder_organisation_enabled = True

        # Enable public share copy if public share directory is provided and is valid directory with MP4 and CDG subdirectories
        if self.public_share_dir is not None:
            if not os.path.isdir(self.public_share_dir):
                raise Exception(f"Public share directory does not exist: {self.public_share_dir}")

            if not os.path.isdir(os.path.join(self.public_share_dir, "MP4")):
                raise Exception(f"Public share directory does not contain MP4 subdirectory: {self.public_share_dir}")

            if not os.path.isdir(os.path.join(self.public_share_dir, "CDG")):
                raise Exception(f"Public share directory does not contain CDG subdirectory: {self.public_share_dir}")

            self.logger.debug(f"Public share directory checks passed, enabling public share copy")
            self.public_share_copy_enabled = True

        # Enable public share rclone if rclone destination is provided
        if self.rclone_destination is not None:
            self.logger.debug(f"Rclone destination provided, enabling rclone sync")
            self.public_share_rclone_enabled = True

        # Tell user which features are enabled, prompt them to confirm before proceeding
        self.logger.info(f"Enabled features:")
        self.logger.info(f" YouTube upload: {self.youtube_upload_enabled}")
        self.logger.info(f" Discord notifications: {self.discord_notication_enabled}")
        self.logger.info(f" Folder organisation: {self.folder_organisation_enabled}")
        self.logger.info(f" Public share copy: {self.public_share_copy_enabled}")
        self.logger.info(f" Public share rclone: {self.public_share_rclone_enabled}")

        self.prompt_user_confirmation_or_raise_exception(
            f"Confirm features enabled log messages above match your expectations for finalisation?",
            "Refusing to proceed without user confirmation they're happy with enabled features.",
            allow_empty=True,
        )

    def authenticate_youtube(self):
        """Authenticate and return a YouTube service object."""
        credentials = None
        pickle_file = "/tmp/karaoke-finalise-token.pickle"

        # Token file stores the user's access and refresh tokens.
        if os.path.exists(pickle_file):
            with open(pickle_file, "rb") as token:
                credentials = pickle.load(token)

        # If there are no valid credentials, let the user log in.
        if not credentials or not credentials.valid:
            if credentials and credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.youtube_client_secrets_file, scopes=["https://www.googleapis.com/auth/youtube"]
                )
                credentials = flow.run_local_server(port=0)  # This will open a browser for authentication

            # Save the credentials for the next run
            with open(pickle_file, "wb") as token:
                pickle.dump(credentials, token)

        return build("youtube", "v3", credentials=credentials)

    def get_channel_id(self):
        youtube = self.authenticate_youtube()

        # Get the authenticated user's channel
        request = youtube.channels().list(part="snippet", mine=True)
        response = request.execute()

        # Extract the channel ID
        if "items" in response:
            channel_id = response["items"][0]["id"]
            return channel_id
        else:
            return None

    def check_if_video_title_exists_on_youtube_channel(self, youtube_title):
        youtube = self.authenticate_youtube()
        channel_id = self.get_channel_id()

        self.logger.info(f"Searching YouTube channel {channel_id} for title: {youtube_title}")
        request = youtube.search().list(part="snippet", channelId=channel_id, q=youtube_title, type="video", maxResults=10)
        response = request.execute()

        # Check if any videos were found
        if "items" in response and len(response["items"]) > 0:
            for item in response["items"]:
                found_title = item["snippet"]["title"]
                similarity_score = fuzz.ratio(youtube_title.lower(), found_title.lower())
                if similarity_score >= 70:  # 70% similarity
                    found_id = item["id"]["videoId"]
                    self.logger.info(
                        f"Potential match found on YouTube channel with ID: {found_id} and title: {found_title} (similarity: {similarity_score}%)"
                    )
                    confirmation = input(f"Is '{found_title}' the video you are finalising? (y/n): ").strip().lower()
                    if confirmation == "y":
                        self.youtube_video_id = found_id
                        self.youtube_url = f"{self.youtube_url_prefix}{self.youtube_video_id}"
                        self.skip_notifications = True
                        return True

        self.logger.info(f"No matching video found with title: {youtube_title}")
        return False

    def truncate_to_nearest_word(self, title, max_length):
        if len(title) <= max_length:
            return title
        truncated_title = title[:max_length].rsplit(" ", 1)[0]
        if len(truncated_title) < max_length:
            truncated_title += " ..."
        return truncated_title

    def upload_final_mp4_to_youtube_with_title_thumbnail(self, artist, title, input_files, output_files):
        self.logger.info(f"Uploading final MP4 to YouTube with title thumbnail...")
        if self.dry_run:
            self.logger.info(
                f'DRY RUN: Would upload {output_files["final_karaoke_mp4"]} to YouTube with thumbnail {input_files["title_jpg"]} using client secrets file: {self.youtube_client_secrets_file}'
            )
        else:
            youtube_title = f"{artist} - {title} (Karaoke)"

            # Truncate title to the nearest whole word and add ellipsis if needed
            max_length = 95
            youtube_title = self.truncate_to_nearest_word(youtube_title, max_length)

            if self.check_if_video_title_exists_on_youtube_channel(youtube_title):
                self.logger.warning(f"Video already exists on YouTube, skipping upload: {self.youtube_url}")
                return

            youtube_description = f"Karaoke version of {artist} - {title} created using karaoke-prep python package."
            if self.youtube_description_file is not None:
                with open(self.youtube_description_file, "r") as f:
                    youtube_description = f.read()

            youtube_category_id = "10"  # Category ID for Music
            youtube_keywords = ["karaoke", "music", "singing", "instrumental", "lyrics", artist, title]

            self.logger.info(f"Authenticating with YouTube...")
            # Upload video to YouTube and set thumbnail.
            youtube = self.authenticate_youtube()

            body = {
                "snippet": {
                    "title": youtube_title,
                    "description": youtube_description,
                    "tags": youtube_keywords,
                    "categoryId": youtube_category_id,
                },
                "status": {"privacyStatus": "public"},
            }

            # Use MediaFileUpload to handle the video file
            media_file = MediaFileUpload(output_files["final_karaoke_mp4"], mimetype="video/mp4", resumable=True)

            # Call the API's videos.insert method to create and upload the video.
            self.logger.info(f"Uploading final MP4 to YouTube...")
            request = youtube.videos().insert(part="snippet,status", body=body, media_body=media_file)
            response = request.execute()

            self.youtube_video_id = response.get("id")
            self.youtube_url = f"{self.youtube_url_prefix}{self.youtube_video_id}"
            self.logger.info(f"Uploaded video to YouTube: {self.youtube_url}")

            # Uploading the thumbnail
            if input_files["title_jpg"]:
                media_thumbnail = MediaFileUpload(input_files["title_jpg"], mimetype="image/jpeg")
                youtube.thumbnails().set(videoId=self.youtube_video_id, media_body=media_thumbnail).execute()
                self.logger.info(f"Uploaded thumbnail for video ID {self.youtube_video_id}")

    def get_next_brand_code(self):
        """
        Calculate the next sequence number based on existing directories in the organised_dir.
        Assumes directories are named with the format: BRAND-XXXX Artist - Title
        """
        max_num = 0
        pattern = re.compile(rf"^{re.escape(self.brand_prefix)}-(\d{{4}})")

        if not os.path.isdir(self.organised_dir):
            raise Exception(f"Target directory does not exist: {self.organised_dir}")

        for dir_name in os.listdir(self.organised_dir):
            match = pattern.match(dir_name)
            if match:
                num = int(match.group(1))
                max_num = max(max_num, num)

        self.logger.info(f"Next sequence number for brand {self.brand_prefix} calculated as: {max_num + 1}")
        next_seq_number = max_num + 1

        return f"{self.brand_prefix}-{next_seq_number:04d}"

    def post_discord_message(self, message, webhook_url):
        """Post a message to a Discord channel via webhook."""
        data = {"content": message}
        response = requests.post(webhook_url, json=data)
        response.raise_for_status()  # This will raise an exception if the request failed
        self.logger.info("Message posted to Discord")

    def find_with_vocals_mov_file(self):
        self.logger.info("Finding input file in current directory ending (With Vocals).mov")

        with_vocals_files = [f for f in os.listdir(".") if self.suffixes["with_vocals_mov"] in f]
        if not with_vocals_files:
            karaoke_files = [f for f in os.listdir(".") if self.suffixes["karaoke_mov"] in f]
            if karaoke_files:
                for file in karaoke_files:
                    new_file = file.replace(self.suffixes["karaoke_mov"], self.suffixes["with_vocals_mov"])

                    self.prompt_user_confirmation_or_raise_exception(
                        f"Found '{file}' but no '(With Vocals)', rename to {new_file} for vocal input?",
                        "Unable to proceed without With Vocals file or user confirmation of rename.",
                        allow_empty=True,
                    )

                    os.rename(file, new_file)
                    self.logger.info(f"Renamed '{file}' to '{new_file}'")
                    return new_file
            else:
                raise Exception("No suitable files found for processing.")
        else:
            return with_vocals_files[0]  # Assuming only one such file is expected

    def choose_instrumental_audio_file(self, base_name):
        self.logger.info(f"Choosing instrumental audio file to use as karaoke audio...")

        search_string = " (Instrumental "
        self.logger.info(f"Searching for files in current directory containing {search_string}")

        all_instrumental_files = [f for f in os.listdir(".") if search_string in f]
        flac_files = set(f.rsplit(".", 1)[0] for f in all_instrumental_files if f.endswith(".flac"))
        mp3_files = set(f.rsplit(".", 1)[0] for f in all_instrumental_files if f.endswith(".mp3"))

        self.logger.debug(f"FLAC files found: {flac_files}")
        self.logger.debug(f"MP3 files found: {mp3_files}")

        # Filter out MP3 files if their FLAC counterpart exists
        filtered_files = [
            f for f in all_instrumental_files if f.endswith(".flac") or (f.endswith(".mp3") and f.rsplit(".", 1)[0] not in flac_files)
        ]

        self.logger.debug(f"Filtered instrumental files: {filtered_files}")

        if not filtered_files:
            raise Exception(f"No instrumental audio files found containing {search_string}")

        if len(filtered_files) == 1:
            return filtered_files[0]

        # Sort the remaining instrumental options alphabetically
        filtered_files.sort(reverse=True)

        self.logger.info(f"Found multiple files containing {search_string}:")
        for i, file in enumerate(filtered_files):
            self.logger.info(f" {i+1}: {file}")

        print()
        response = input(f"Choose instrumental audio file to use as karaoke audio: [1]/{len(filtered_files)}: ").strip().lower()
        if response == "":
            response = "1"

        try:
            response = int(response)
        except ValueError:
            raise Exception(f"Invalid response to instrumental audio file choice prompt: {response}")

        if response < 1 or response > len(filtered_files):
            raise Exception(f"Invalid response to instrumental audio file choice prompt: {response}")

        return filtered_files[response - 1]

    def get_names_from_withvocals(self, with_vocals_file):
        self.logger.info(f"Getting artist and title from {with_vocals_file}")

        base_name = with_vocals_file.replace(self.suffixes["with_vocals_mov"], "")
        artist, title = base_name.split(" - ", 1)
        return base_name, artist, title

    def execute_command(self, command, description):
        self.logger.info(description)
        if self.dry_run:
            self.logger.info(f"DRY RUN: Would run command: {command}")
        else:
            self.logger.info(f"Running command: {command}")
            os.system(command)

    def remux_and_encode_output_video_files(self, with_vocals_file, input_files, output_files):
        self.logger.info(f"Remuxing and encoding output video files...")

        # Check if output files already exist, if so, ask user to overwrite or skip
        if os.path.isfile(output_files["karaoke_mov"]) and os.path.isfile(output_files["final_karaoke_mp4"]):
            if not self.prompt_user_bool(
                f"Found existing Final Karaoke output file: {output_files['final_karaoke_mp4']}. Overwrite (y) or skip (n)?",
            ):
                self.logger.info(f"Skipping Karaoke MOV remux and Final MP4 render, existing files will be used.")
                return

        # Remux the synced video with the instrumental audio to produce an instrumental karaoke MOV file
        remux_ffmpeg_command = f'{self.ffmpeg_base_command} -an -i "{with_vocals_file}" -vn -i "{input_files["instrumental_audio"]}" -c:v copy -c:a aac "{output_files["karaoke_mov"]}"'
        self.execute_command(remux_ffmpeg_command, "Remuxing video with instrumental audio")

        # Quote file paths to handle special characters
        title_mov_file = shlex.quote(os.path.abspath(input_files["title_mov"]))
        karaoke_mov_file = shlex.quote(os.path.abspath(output_files["karaoke_mov"]))
        output_final_mp4_file = shlex.quote(output_files["final_karaoke_mp4"])

        # Use ffmpeg concat filter to join the title video and the karaoke video to produce the final MP4
        join_ffmpeg_command = f'{self.ffmpeg_base_command} -i {title_mov_file} -i {karaoke_mov_file} -filter_complex "[0:v:0][0:a:0][1:v:0][1:a:0]concat=n=2:v=1:a=1[outv][outa]" -map "[outv]" -map "[outa]" -c:v libx264 -c:a aac_at -q:a 14 {output_final_mp4_file}'
        self.logger.debug(f"Running command: {join_ffmpeg_command}")
        self.execute_command(join_ffmpeg_command, "Joining title and instrumental videos")

        # Prompt user to check final MP4 file before proceeding
        self.prompt_user_confirmation_or_raise_exception(
            f"Final MP4 file created: {output_files['final_karaoke_mp4']}, please check it! Proceed?",
            "Refusing to proceed without user confirmation they're happy with the Final MP4.",
            allow_empty=True,
        )

    def create_cdg_zip_file(self, input_files, output_files):
        self.logger.info(f"Creating CDG ZIP file...")

        # Check if CDG file already exists, if so, ask user to overwrite or skip
        if os.path.isfile(output_files["final_karaoke_zip"]):
            if not self.prompt_user_bool(
                f"Found existing CDG ZIP file: {output_files['final_karaoke_zip']}. Overwrite (y) or skip (n)?",
            ):
                self.logger.info(f"Skipping CDG ZIP file creation, existing file will be used.")
                return

        # Create the ZIP file containing the MP3 and CDG files
        if self.dry_run:
            self.logger.info(f"DRY RUN: Would create CDG ZIP file: {output_files['final_karaoke_zip']}")
        else:
            self.logger.info(f"Creating ZIP file containing {input_files['karaoke_mp3']} and {input_files['karaoke_cdg']}")
            with zipfile.ZipFile(output_files["final_karaoke_zip"], "w") as zipf:
                zipf.write(input_files["karaoke_mp3"], os.path.basename(input_files["karaoke_mp3"]))
                zipf.write(input_files["karaoke_cdg"], os.path.basename(input_files["karaoke_cdg"]))

            if not os.path.isfile(output_files["final_karaoke_zip"]):
                raise Exception(f"Failed to create CDG ZIP file: {output_files['final_karaoke_zip']}")

            self.logger.info(f"CDG ZIP file created: {output_files['final_karaoke_zip']}")

    def move_files_to_brand_code_folder(self, brand_code, artist, title, output_files):
        self.logger.info(f"Moving files to new brand-prefixed directory...")

        self.new_brand_code_dir_path = os.path.join(self.organised_dir, f"{brand_code} - {artist} - {title}")

        self.prompt_user_confirmation_or_raise_exception(
            f"Move files to new brand-prefixed directory {self.new_brand_code_dir_path} and delete current dir?",
            "Refusing to move files without user confirmation of move.",
            allow_empty=True,
        )

        orig_dir = os.getcwd()
        os.chdir(os.path.dirname(orig_dir))
        self.logger.info(f"Changed dir to parent directory: {os.getcwd()}")

        if self.dry_run:
            self.logger.info(f"DRY RUN: Would move original directory {orig_dir} to: {self.new_brand_code_dir_path}")
        else:
            os.rename(orig_dir, self.new_brand_code_dir_path)

    def copy_final_files_to_public_share_dirs(self, brand_code, base_name, output_files):
        self.logger.info(f"Copying final MP4 and ZIP to public share directory...")

        # Validate public_share_dir is a valid folder with MP4 and CDG subdirectories
        if not os.path.isdir(self.public_share_dir):
            raise Exception(f"Public share directory does not exist: {self.public_share_dir}")

        if not os.path.isdir(os.path.join(self.public_share_dir, "MP4")):
            raise Exception(f"Public share directory does not contain MP4 subdirectory: {self.public_share_dir}")

        if not os.path.isdir(os.path.join(self.public_share_dir, "CDG")):
            raise Exception(f"Public share directory does not contain CDG subdirectory: {self.public_share_dir}")

        if brand_code is None:
            raise Exception(f"New track prefix was not set, refusing to copy to public share directory")

        dest_mp4_dir = os.path.join(self.public_share_dir, "MP4")
        dest_cdg_dir = os.path.join(self.public_share_dir, "CDG")
        os.makedirs(dest_mp4_dir, exist_ok=True)
        os.makedirs(dest_cdg_dir, exist_ok=True)

        dest_mp4_file = os.path.join(dest_mp4_dir, f"{brand_code} - {base_name}.mp4")
        dest_zip_file = os.path.join(dest_cdg_dir, f"{brand_code} - {base_name}.zip")

        if self.dry_run:
            self.logger.info(f"DRY RUN: Would copy final MP4 and ZIP to {dest_mp4_file} + .cdg")
        else:
            shutil.copy2(output_files["final_karaoke_mp4"], dest_mp4_file)
            shutil.copy2(output_files["final_karaoke_zip"], dest_zip_file)
            self.logger.info(f"Copied final files to public share directory")

    def sync_public_share_dir_to_rclone_destination(self):
        self.logger.info(f"Syncing public share directory to rclone destination...")

        # Delete .DS_Store files recursively before syncing
        for root, dirs, files in os.walk(self.public_share_dir):
            for file in files:
                if file == ".DS_Store":
                    file_path = os.path.join(root, file)
                    os.remove(file_path)
                    self.logger.info(f"Deleted .DS_Store file: {file_path}")

        rclone_cmd = f"rclone sync -v '{self.public_share_dir}' '{self.rclone_destination}'"
        self.execute_command(rclone_cmd, "Syncing with cloud destination")

    def post_discord_notification(self):
        self.logger.info(f"Posting Discord notification...")

        if self.skip_notifications:
            self.logger.info(f"Skipping Discord notification as video was previously uploaded to YouTube")
            return

        if self.dry_run:
            self.logger.info(
                f"DRY RUN: Would post Discord notification for youtube URL {self.youtube_url} using webhook URL: {self.discord_webhook_url}"
            )
        else:
            discord_message = f"New upload: {self.youtube_url}"
            self.post_discord_message(discord_message, self.discord_webhook_url)

    def execute_optional_features(self, artist, title, base_name, input_files, output_files):
        self.logger.info(f"Executing optional features...")

        if self.youtube_upload_enabled:
            try:
                self.upload_final_mp4_to_youtube_with_title_thumbnail(artist, title, input_files, output_files)
            except Exception as e:
                self.logger.error(f"Failed to upload video to YouTube: {e}")
                print("Please manually upload the video to YouTube.")
                print()
                self.youtube_video_id = input("Enter the manually uploaded YouTube video ID: ").strip()
                self.youtube_url = f"{self.youtube_url_prefix}{self.youtube_video_id}"
                self.logger.info(f"Using manually provided YouTube video ID: {self.youtube_video_id}")

            if self.discord_notication_enabled:
                self.post_discord_notification()

        if self.folder_organisation_enabled:
            self.brand_code = self.get_next_brand_code()

            if self.public_share_copy_enabled:
                self.copy_final_files_to_public_share_dirs(self.brand_code, base_name, output_files)

            if self.public_share_rclone_enabled:
                self.sync_public_share_dir_to_rclone_destination()

            self.move_files_to_brand_code_folder(self.brand_code, artist, title, output_files)

    def process(self):
        if self.dry_run:
            self.logger.warning("Dry run enabled. No actions will be performed.")

        # Check required input files and parameters exist, get user to confirm features before proceeding
        self.validate_input_parameters_for_features()

        with_vocals_file = self.find_with_vocals_mov_file()
        base_name, artist, title = self.get_names_from_withvocals(with_vocals_file)

        instrumental_audio_file = self.choose_instrumental_audio_file(base_name)

        input_files = self.check_input_files_exist(base_name, with_vocals_file, instrumental_audio_file)
        output_files = self.prepare_output_filenames(base_name)

        self.create_cdg_zip_file(input_files, output_files)
        self.remux_and_encode_output_video_files(with_vocals_file, input_files, output_files)

        self.execute_optional_features(artist, title, base_name, input_files, output_files)

        return {
            "artist": artist,
            "title": title,
            "video_with_vocals": with_vocals_file,
            "video_with_instrumental": output_files["karaoke_mov"],
            "final_video": output_files["final_karaoke_mp4"],
            "final_zip": output_files["final_karaoke_zip"],
            "youtube_url": self.youtube_url,
            "brand_code": self.brand_code,
            "new_brand_code_dir_path": self.new_brand_code_dir_path,
        }
