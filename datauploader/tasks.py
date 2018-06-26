"""
Asynchronous tasks that update data in Open Humans.
These tasks:
  1. delete any current files in OH if they match the planned upload filename
  2. adds a data file
"""
import logging
import json
import tempfile
import requests
import os
from celery import shared_task
from django.conf import settings
from open_humans.models import OpenHumansMember
from datetime import datetime, timedelta
from demotemplate.settings import rr
from requests_respectful import RequestsRespectfulRateLimitedError
from ohapi import api
import arrow

# Set up logging.
logger = logging.getLogger(__name__)

GITHUB_API_BASE = 'https://api.github.com'
GITHUB_API_STORY = GITHUB_API_BASE + '/feeds'
GITHUB_API_REPO = GITHUB_API_BASE + '/user/repos'
GITHUB_API_STARS = GITHUB_API_BASE + '/user/starred'

@shared_task
def process_github(oh_id):
    """
    Update the github file for a given OH user
    """
    logger.debug('Starting github processing for {}'.format(oh_id))
    oh_member = OpenHumansMember.objects.get(oh_id=oh_id)
    oh_access_token = oh_member.get_access_token(
                            client_id=settings.OPENHUMANS_CLIENT_ID,
                            client_secret=settings.OPENHUMANS_CLIENT_SECRET)
    github_data = get_existing_github(oh_access_token)
    github_member = oh_member.datasourcemember
    github_access_token = github_member.get_access_token(
                            client_id=settings.GITHUB_CLIENT_ID,
                            client_secret=settings.GITHUB_CLIENT_SECRET)
    update_github(oh_member, github_data)

def update_github(oh_member, github_access_token, github_data):
    try:
        start_date = get_start_date(github_data, github_access_token)
        start_date = datetime.strptime(start_date, "%Y%m%d")
        start_date_iso = start_date.isocalendar()[:2]
        github_data = remove_partial_data(github_data, start_date_iso)
        stop_date_iso = (datetime.utcnow()
                         + timedelta(days=7)).isocalendar()[:2]
        while start_date_iso != stop_date_iso:
            print('processing {}-{} for member {}'.format(start_date_iso[0],
                                                          start_date_iso[1],
                                                          oh_member.oh_id))
            query = GITHUB_API_STORY + \
                     '/{0}-W{1}?trackPoints=true&access_token={2}'.format(
                        start_date_iso[0],
                        start_date_iso[1],
                        github_access_token
                     )
            response = rr.get(query, realms=['github'])
            github_data += response.json()
            start_date = start_date + timedelta(days=7)
            start_date_iso = start_date.isocalendar()[:2]
        print('successfully finished update for {}'.format(oh_member.oh_id))
        github_member = oh_member.datasourcemember
        github_member.last_updated = arrow.now().format()
        github_member.save()
    except RequestsRespectfulRateLimitedError:
        logger.debug(
            'requeued processing for {} with 60 secs delay'.format(
                oh_member.oh_id)
                )
        process_github.apply_async(args=[oh_member.oh_id], countdown=61)
    finally:
        replace_github(oh_member, github_data)


def replace_github(oh_member, github_data):
    # delete old file and upload new to open humans
    tmp_directory = tempfile.mkdtemp()
    metadata = {
        'description':
        'Github activity feed, repository contents and stars data.',
        'tags': ['demo', 'dummy', 'test'],
        'updated_at': str(datetime.utcnow()),
        }
    out_file = os.path.join(tmp_directory, 'github-data.json')
    logger.debug('deleted old file for {}'.format(oh_member.oh_id))
    api.delete_file(oh_member.access_token,
                    oh_member.oh_id,
                    file_basename="dummy-data.json")
    with open(out_file, 'w') as json_file:
        json.dump(github_data, json_file)
        json_file.flush()
    api.upload_aws(out_file, metadata,
                   oh_member.access_token,
                   project_member_id=oh_member.oh_id)
    logger.debug('uploaded new file for {}'.format(oh_member.oh_id))


def remove_partial_data(github_data, start_date):
    remove_indexes = []
    for i, element in enumerate(github_data):
        element_date = datetime.strptime(
                                element['date'], "%Y%m%d").isocalendar()[:2]
        if element_date == start_date:
            remove_indexes.append(i)
    for index in sorted(remove_indexes, reverse=True):
        del github_data[index]
    return github_data


def get_start_date(github_data, github_access_token):
    if github_data == []:
        url = GITHUB_API_BASE + "/user/profile?access_token={}".format(
                                        github_access_token
        )
        response = rr.get(url, wait=True, realms=['github'])
        return response.json()['profile']['firstDate']
    else:
        return github_data[-1]['date']


def get_existing_github(oh_access_token):
    member = api.exchange_oauth2_member(oh_access_token)
    for dfile in member['data']:
        if 'Github' in dfile['metadata']['tags']:
            # get file here and read the json into memory
            tf_in = tempfile.NamedTemporaryFile(suffix='.json')
            tf_in.write(requests.get(dfile['download_url']).content)
            tf_in.flush()
            github_data = json.load(open(tf_in.name))
            return github_data
    return []
