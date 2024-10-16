import csv
from random import randint
from googleapiclient.errors import HttpError

from google_drive.google_api_services import GoogleDriveManager
import time
from datetime import datetime, date
from dateutil.relativedelta import relativedelta
from typing import Optional
import io
import logging

from config.settings import headers
from controllers.logincontroller import LoginController
from controllers.logincontrollerinterface import LoginControllerInterface
from models.sessionmanager import SessionManager
from bs4 import BeautifulSoup, PageElement

from models.stringlist import StringLists
from services.blacklistservice import BlacklistService
from utils.profile_id_extractor import get_profile_id, get_offer_url_from_chat_page


class ReminderService:
    def __init__(self, loginController: Optional[LoginControllerInterface] = None):
        self.blService = BlacklistService()
        self.message = None
        self.username = None
        self.password = None
        self.notifier = None
        self.stopped = False
        self.loginController = loginController if loginController else LoginController()
        self.google_drive_manager = GoogleDriveManager()

    def run_reminder_service(self, reminder_frequency: Optional[str] = None,
                             reminder_message: Optional[str] = None):

        DEFAULT_FREQUENCY = 3
        DEFAULT_MESSAGE = f'''
                                Hello,
                                
                                Were you able to check my request and if so, could you tell me if you are interested?
                                I am looking forward to your response,
                                
                          '''

        reminder_frequency, reminder_message = (
            reminder_frequency or None,
            reminder_message or None,
        )

        if not reminder_frequency or not reminder_message:
            stored_frequency, stored_message = self.google_drive_manager.read_frequency_data()
            reminder_frequency = reminder_frequency or stored_frequency or DEFAULT_FREQUENCY
            reminder_message = reminder_message or stored_message or DEFAULT_MESSAGE

        if reminder_frequency and reminder_message:
            self.google_drive_manager.write_frequency_data(reminder_frequency, reminder_message)

        print('Reminder frequency: ', reminder_frequency)
        print('Reminder message: ', reminder_message)

        chats_with_no_response = self.get_unanswered_chats(reminder_frequency)
        print("Chats with no response:", chats_with_no_response)

        if not reminder_frequency:
            reminder_frequency = 3

        if chats_with_no_response:
            self.csv_handler(chats_with_no_response, reminder_frequency, reminder_message)


        self.stop_reminder_service()


    def get_all_contacted_names(self) -> StringLists | bool:

        """
        Get all contacted names

        return: StringLists object with names and urls
        """
        print("Collecting all chats...")
        urls = []
        url = 'https://www.nlvoorelkaar.nl/mijn-pagina/berichten'
        urls.append(url)
        response = SessionManager.get_session().get(url, headers=headers)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            paginator = soup.find('div', {'class': 'paginator'})

            if paginator:
                pages = paginator.find_all('a')
                for page in pages:
                    if not page.text.isalpha():
                        page_number = int(page.text)
                        if page_number > 1:
                            url = f'https://www.nlvoorelkaar.nl/mijn-pagina/berichten?p={page_number}'
                            urls.append(url)
            volunteers_names = []
            for url in urls:
                response = SessionManager.get_session().get(url, headers=headers)
                soup = BeautifulSoup(response.text, 'html.parser')
                volunteers = soup.find_all('a', {'aria-labelledby': 'message-of-label'})
                for volunteer_name in volunteers:
                    volunteers_names.append(volunteer_name.text)

            result = StringLists(volunteers_names, urls)
            return result



    @staticmethod
    def check_with_frequency(str_date, frequency):
        """
            Check if the last reminder to this chat was sent more than 3 days ago

            param: str_date: string date in format 'YYYY-MM-DD'

            return: bool: True if the last reminder was sent more than specified frequency ago, False otherwise
        """

        today = datetime.now()
        last_check = datetime.strptime(str_date, '%Y-%m-%d')
        delta = today - last_check
        return delta.days >= int(frequency)

    def construct_message(self, chat_url: str):
        """
            Construct the message to be sent to the receiver

            param: chat_url: string url of the chat

            return: string: message to be sent
        """

        sender_name = self.get_sender_name()
        receiver_name = self.get_receiver_name(chat_url)
        message = self.format_message(sender_name, receiver_name)
        return message

    @staticmethod
    def format_message(sender_name: str, receiver_name: str):
        """
            Format the message to be sent to the receiver placing the names in the message

            param: sender_name: string name of the sender
            param: receiver_name: string name of the receiver

            return: string: formatted message
        """
        message = f'''
    Hello {receiver_name},
    
    Were you able to check my request and if so, could you tell me if you are interested?
    I am looking forward to your response,
    
    Yours Sincerely,
    {sender_name}
        '''
        return message

    def send_reminder(self, chat_url: str, message: Optional[str] = None):
        """
                Send a reminder to the receiver of the chat

                param: chat_url: string url of the chat
                param: message: string message to be sent

                return: bool: True if the message was sent successfully, False otherwise
            """

        profile_url = get_offer_url_from_chat_page(chat_url)
        profile_id = get_profile_id(profile_url)

        if self.blService.check_if_was_blacklisted(profile_id) or not profile_id:
            print(f"Volunteer with id {profile_id} was blacklisted")
            return

        try:
            response = SessionManager.get_session().get(chat_url, headers=headers)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'html.parser')
                message_token = soup.find('input', {'name': 'message[_token]'})['value']
                message_loaded = soup.find('input', {'name': 'message[loaded]'})['value']

                data = {
                    'message[body]': message,
                    'message[dusdat]': '',
                    'message[_token]': message_token,
                    'message[loaded]': message_loaded
                }

                time.sleep(randint(45, 75))
                response = SessionManager.get_session().post(chat_url, data=data, headers=headers)

                if response.status_code == 200:
                    print(f'Reminder sent to {chat_url}', "Text: ", message)


        except Exception as e:
            logging.error(f'Error while sending reminder to chat with url {chat_url}: {str(e)}')


    def csv_handler(self, chats_with_no_response, reminder_frequency: int, reminder_message: Optional[str] = None):
        if len(chats_with_no_response)<1:
            print("No chats with no response")
            return

        file_id = self.google_drive_manager.find_file_id_by_name("chats_no_response.csv")
        if file_id:
            try:
                file_content = self.google_drive_manager.download_file_content(file_id)
            except HttpError as error:
                print(f"An error occurred while downloading the file: {error}")
                return
        else:
            file_content = None

        # Read the CSV content
        rows_in_file = []
        if file_content:
            file_stream = io.StringIO(file_content.decode('utf-8'))
            reader = csv.reader(file_stream)
            rows_in_file = list(reader)

        updated_rows = []
        today = date.today()
        six_months_ago = today - relativedelta(months=6)

        for chat_url in chats_with_no_response:
            chat_exists = False
            for i, row in enumerate(rows_in_file):
                if len(row) > 0 and row[0] == chat_url:
                    chat_exists = True
                    last_contact_date = datetime.strptime(row[1], '%Y-%m-%d').date()
                    if self.check_with_frequency(row[1], reminder_frequency) and int(row[2]) < 4:
                        rows_in_file[i] = [chat_url, datetime.now().strftime('%Y-%m-%d'), str(int(row[2]) + 1)]
                        reminder_msg = reminder_message if reminder_message else self.construct_message(chat_url)
                        self.send_reminder(chat_url, reminder_msg)

                    elif self.check_with_frequency(row[1], reminder_frequency) and int(
                            row[2]) == 4 and not last_contact_date <= six_months_ago:
                        print(f"Chat {chat_url} has been banned to send more reminders")
                    elif not self.check_with_frequency(row[1], reminder_frequency):
                        print(f"Message is not older than {reminder_frequency} days for {chat_url}")
                    elif last_contact_date <= six_months_ago and int(row[2]) == 4:
                        rows_in_file[i] = [chat_url, datetime.now().strftime('%Y-%m-%d'), str(5)]
                        new_row = [chat_url, datetime.now().strftime('%Y-%m-%d'), '0']
                        updated_rows.append(new_row)
                        reminder_msg = reminder_message if reminder_message else self.construct_message(chat_url)
                        self.send_reminder(chat_url, reminder_msg)
                        print(f"New help request after 12 months for {chat_url}")

            if not chat_exists:
                new_row = [chat_url, datetime.now().strftime('%Y-%m-%d'), '0']
                updated_rows.append(new_row)
                reminder_msg = reminder_message if reminder_message else self.construct_message(chat_url)
                self.send_reminder(chat_url, reminder_msg)
                print(f"Sent message to {chat_url}")

        # Combine updated rows with existing ones, removing duplicates
        unique_rows = {tuple(row) for row in updated_rows + rows_in_file}

        # Write the updated CSV content to a string buffer
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerows(unique_rows)
        output.seek(0)

        # Upload the updated file to Google Drive
        try:
            self.google_drive_manager.upload_file_content(output.getvalue().encode('utf-8'),
                                                          "chats_no_response.csv")
        except HttpError as error:
            print(f"An error occurred while uploading the file: {error}")

    def construct_message(self, chat_url: str) -> str:
        """
        Construct the message to be sent to the receiver

        :param chat_url: string url of the chat
        :return: string: message to be sent
        """
        sender_name = self.get_sender_name()
        receiver_name = self.get_receiver_name(chat_url)
        message = self.format_message(sender_name, receiver_name)
        return message

    def get_unanswered_chats(self, reminder_frequency: int):

        """
            Check each chat for a response

            return: list of chat_urls with no response
            """
        names_urls_object = self.get_all_contacted_names()
        chats_with_no_response = set()

        try:
            volunteer_names = names_urls_object.names

            urls = names_urls_object.pages


            for url in urls:
                response = SessionManager.get_session().get(url, headers=headers)

                if response.status_code == 200:
                    soup = BeautifulSoup(response.text, 'html.parser')

                    chats = soup.find_all('li', {'class': 'list__item list__item--messages'})

                    for chat in chats:
                        chat_url = chat.find('a', {'class': 'button button--primary button--detail'})
                        if chat_url:
                            chat_url = "https://www.nlvoorelkaar.nl" + chat_url['href']

                            response = SessionManager.get_session().get(chat_url, headers=headers)
                            soup = BeautifulSoup(response.text, 'html.parser')
                            message_metas = soup.find_all('p', {'class': 'meta conversation__meta'})

                            if self.check_last_message_date(message_metas, int(reminder_frequency)):
                                message_authors = []
                                for message_meta in message_metas:
                                    author = message_meta.text.split(' ')[0]
                                    if author in volunteer_names and not self.check_60_days(message_meta):
                                        message_authors.append(author)

                                if len(message_authors) == 0:
                                    chats_with_no_response.add(chat_url)

                                    continue

                            else:
                                pass



                else:
                    logging.error(f'Error while checking unanswered messages: Could not get messages page')
                    return False
        except Exception as e:
            print(f'Error while checking unanswered messages: {str(e)}')
            return False
        return chats_with_no_response

    def get_sender_name(self):
        """
                Get the name of the sender of the chat

                return: string: name of the sender
            """
        try:
            url = 'https://www.nlvoorelkaar.nl/en/mijn-pagina/profiel'
            response = SessionManager.get_session().get(url, headers=headers)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'html.parser')
                name = soup.find('input', {'id': 'user_profile_firstName'})['value']
                return name
        except Exception as e:
            logging.error(f'Error while getting sender name: {str(e)}')
            return False

    def get_receiver_name(self, chat_url: str):
        """
            Get the name of the receiver of the chat

            param: chat_url: string url of the chat

            return: string: name of the receiver

        """

        if chat_url:
            try:

                response = SessionManager.get_session().get(chat_url, headers=headers)
                if response.status_code == 200:
                    soup = BeautifulSoup(response.text, 'html.parser')
                    # find by xpath
                    receiver_name = soup.find('dl', {
                        'class': 'list__definition list__definition--horizontal list__definition--plain list-definition--small'}).text.split(
                        '\n')[2].strip()
                    return receiver_name
            except Exception as e:
                logging.error(f'Error while getting receiver name: {str(e)}')
                return False

    def check_last_message_date(self, message_metas, reminder_frequency: int):
        """
            Check if the last message on the page was sent more than {reminder_frequency} days ago

            param: chat_url: string url of the chat
            param: reminder_frequency: int frequency of the reminders

            return: bool: True if the last message was sent more than 3 days ago, False otherwise
        """

        try:

            last_message_date = message_metas[-1].text[-16:-6:1]

            today = datetime.now()
            last_message_date = datetime.strptime(last_message_date, '%d.%m.%Y')

            delta = today - last_message_date

            return delta.days >= reminder_frequency
        except Exception as e:
            print(f'Error while checking last message date: {str(e)}')
            return False

    def check_60_days(self, message_meta: PageElement):
        """
            Check if the last message on the page was sent more than 60 days ago

            param: message_meta: string meta of the message

            return: bool: True if the last message was sent more than 60 days ago, False otherwise
        """
        try:
            last_message_date = message_meta.text[-16:-6:1]

            today = datetime.now()
            last_message_date = datetime.strptime(last_message_date, '%d.%m.%Y')

            delta = today - last_message_date

            return delta.days >= 60
        except Exception as e:
            print(f'Error while checking last message date: {str(e)}')
            return False

    def stop_reminder_service(self):
        """
        Stop the reminder service
        """
        self.stopped = True
