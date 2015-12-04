#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright 2013 Palantir Technologies

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

'''
Asana Mailer is a script that retrieves metadata from an Asana project via
Asana's REST API to generate a plaintext and HTML email using Jinja2 templates.

:copyright: (c) 2013 by Palantir Technologies
:license: Apache 2.0, see LICENSE for more details.
'''

import argparse
import codecs
import datetime
import json
import logging
import smtplib
import string
import dateutil.parser
import dateutil.tz
import premailer
import requests
import abc

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from jinja2 import Environment, FileSystemLoader


def init_logging():
    log = logging.getLogger('asana_mailer')
    log.setLevel(logging.INFO)

    logging_formatter = logging.Formatter(
        '%(asctime)s %(levelname)s [%(name)s]: %(message)s '
        '[%(filename)s:%(lineno)d]')

    file_handler = logging.FileHandler('asana_mailer.log', encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging_formatter)

    log.addHandler(file_handler)
    return log


log = init_logging()


class AsanaAPI(object):
    '''The class for making calls to Asana's REST API.

    The Asana class represents the infrastructure for storing a user's API key
    and making calls to Asana's API. It is utilized for generating the Project
    and its contained objects (Section, Task, etc.).
    '''

    asana_api_url = 'https://app.asana.com/api/1.0/'
    project_endpoint = 'projects/{project_id}'
    project_tasks_endpoint = 'projects/{project_id}/tasks'
    task_stories_endpoint = 'tasks/{task_id}/stories'
    task_subtasks_endpoint = 'tasks/{task_id}/subtasks'

    def __init__(self, api_key):
        self.api_key = api_key

    def get(self, endpoint_name, path_vars=None, expand=None, params=None):
        '''Makes a call to Asana's API.

        :param endpoint_name: The endpoint attribute to connect to
        :param **kwargs: The keyword arguments necessary for retrieving data
        from a particular endpoint
        '''
        endpoint = getattr(type(self), '{0}_endpoint'.format(endpoint_name))
        if path_vars is not None:
            endpoint = endpoint.format(**path_vars)
        url = '{0}{1}'.format(type(self).asana_api_url, endpoint)
        log.info('Making API Call to {0}'.format(url))
        if expand:
            if params is None:
                params = {}
            if 'opt_expand' not in params:  # Don't overwrite parameters
                params['opt_expand'] = expand
        response = requests.get(url, params=params, auth=(self.api_key, ''))
        if response.status_code == requests.codes.ok:
            return response.json()[u'data']
        else:
            log.error('Asana API Returned Non-OK (200) Response')
            if response.content:
                try:
                    log.error('Response Content:\n{0}'.format(
                        json.dumps(json.loads(response.content), indent=2)))
                except (TypeError, ValueError):
                    # If the error content isn't JSON, don't log it.
                    pass
            response.raise_for_status()


class TaskContainer(object):
    '''An object that represents an Asana object that can contain tasks and basic metadata.

    It is intended to support Asana Projects and Tasks, both of which can contain one or
    more tasks organized into sections. It also provides storage for common Asana metadata
    including id, name and descrition. It is intended to be used via its 
    populate_from_asana_task_json method, which parses the data return by Asana's API calls
    that return collections of tasks. It handles creating
    sections and their associated task objects, as well as filtering tasks and
    sections.
    '''

    def __init__(self, id, name, description, sections=None):
        self.id = id
        self.name = name
        self.description = description
        self.sections = sections
        self.current_status = None
        if self.sections is None:
            self.sections = []

    @abc.abstractmethod
    def get_tasks_json(self, asana, current_time_utc, completed_lookback_hours):
        '''Retrieve from Asana API the json representation of this object's tasks.

        :param asana: The initialized Asana object that makes API calls
        :param completed_lookback_hours: An amount in hours to look back for
            completed tasks
        '''
        return
        
    @staticmethod
    def parse_tasks_into_sections_and_comments(
        asana, tasks_json, current_time_utc, task_filters=None,
        section_filters=None):
        '''Creates a Project utilizing data from Asana.

        Using filters, a project attempts to optimize the calls it makes to
        Asana's API. After the JSON data has been collected, it is then parsed
        into Task and Section objects, and then filtered again in order to
        perform filtering that is only possible post-parsing.

        :param asana: The initialized Asana object that makes API calls
        :param tasks_json: The tasks to parse and import, in Asana's json format
        :param task_filters: A list of tag filters for filtering out tasks
        :param section_filters: A list of sections to filter out tasks
        :return: a (Sections, Comments) tuple where Sections is a list of 
        sections containing tasks, and Comments is a dictionary indexed by task id
        '''
        task_comments = {}

        current_section = None
        log.info('Starting API Calls for Task Comments')
        for task in tasks_json:
            if task[u'name'].endswith(':'):
                current_section = task[u'name']
            # Optimize calls to API
            if section_filters and current_section not in section_filters:
                continue
            tag_names = frozenset((tag[u'name'] for tag in task[u'tags']))
            if task_filters and not tag_names >= task_filters:
                continue
            task_id = unicode(task[u'id'])
            log.info('Getting task comments for task: {0}'.format(task_id))
            task_stories = asana.get('task_stories', {'task_id': task_id})
            current_task_comments = [
                story for story in task_stories if
                story[u'type'] == u'comment']
            if current_task_comments:
                task_comments[task_id] = current_task_comments
        return (tasks_json, task_comments)

    def populate_tasks_from_json(self,
        asana, task_json, current_time_utc, task_filters=None,
        section_filters=None, completed_lookback_hours=None, include_subtasks=True):
            
        subtask_comments = {}
#        current_task_subtasks_json = self.get_tasks_json(asana, current_time_utc, completed_lookback_hours)
        (task_json, subtask_comments) = TaskContainer.parse_tasks_into_sections_and_comments(asana, task_json,
                current_time_utc, task_filters, section_filters)
        if task_json:
            subtasks_in_sections = Section.create_sections(task_json, subtask_comments)
            self.add_sections(subtasks_in_sections)
        self.filter_tasks(current_time_utc, section_filters=section_filters,
            task_filters=task_filters)
        if include_subtasks:
            self.populate_subtasks(asana, current_time_utc, task_filters, section_filters, completed_lookback_hours, include_subtasks)

    def populate_subtasks(self,
        asana, current_time_utc, task_filters=None,
        section_filters=None, completed_lookback_hours=None, include_subtasks=True):

        log.info('Starting adding task subtasks')
        for section in self.sections:
            for task in section.tasks:
                current_task_subtasks_json = task.get_tasks_json(asana, current_time_utc, completed_lookback_hours)
                task.populate_tasks_from_json(asana, current_task_subtasks_json, current_time_utc, task_filters, section_filters, completed_lookback_hours, include_subtasks)
#                for sec in task.sections:
#                    for t in sec.tasks:
#                        print "{0} < {1} < {2}".format(t.name, sec.name, task.name)
                

    def add_section(self, section):
        '''Add a section to the project.

        :param section: The section to add to the project
        '''
        if isinstance(section, Section):
            self.sections.append(section)

    def add_sections(self, sections):
        '''Add multiple sections to the project.

        :param sections: A list of sections to add to the project
        '''
        self.sections.extend(
            (section for section in sections if isinstance(section, Section)))

    def set_current_status(self, current_status_json):
        '''Add a status for the project.

        :param current_status_json: A set of various Asana status settings
        '''
        if current_status_json:
            self.current_status_author = current_status_json[u"author"][u"name"]
            self.current_status_color = current_status_json[u"color"]
            self.current_status_text = current_status_json[u"text"]
            self.current_status_html = current_status_json[u"html_text"]
            self.current_status_modified_at = current_status_json[u"modified_at"]
        else:
            self.current_status_author = None
            self.current_status_color = None
            self.current_status_text = None
            self.current_status_html = None
            self.current_status_modified_at = None
        
    def filter_tasks(
            self, current_time_utc, section_filters=None, task_filters=None):
        '''Filter out tasks based on filters based on filter criteria.

        :param sections_filters: A list of sections to filter the Project on
        :param task_filters: A list of tags to filter the Project's tasks on
        :param current_time_utc: The current time in UTC
        '''
        # Section Filters
        if section_filters:
            log.info('Filtering sections by section filters: ({0})'.format(
                ','.join(section_filters)))
            self.sections[:] = [
                s for s in self.sections if s.name in section_filters]
        # Task (Tag) Filters
        if task_filters:
            log.info('Filtering tasks by tag filters: {0}'.format(
                task_filters))
            for section in self.sections:
                section.tasks[:] = [
                    task for task in section.tasks
                    if task.tags_in(task_filters)]
        # Remove Empty Sections
        log.info('Removing empty sections')
        self.sections[:] = [s for s in self.sections if s.tasks]


class Project(TaskContainer):
    '''An object that represents an Asana Project and its metadata.

    It is intended to be created via its create_project method, which utilizes
    an Asana object to make calls to Asana's API. It also handles creating
    sections and their associated task objects, as well as filtering tasks and
    sections.
    '''

    def __init__(self, task_id, name, description, sections=None):
        super(Project, self).__init__(task_id, name, description, sections)
       # super(Project, self).__init__(id, name, description, sections)
#        self.id = id
#        self.name = name
#        self.description = description
#        self.sections = sections
#        if self.sections is None:
#            self.sections = []

    def get_tasks_json(self, asana, current_time_utc, completed_lookback_hours):
        tasks_params = {}
        if completed_lookback_hours:
            completed_since = (current_time_utc - datetime.timedelta(
                hours=completed_lookback_hours)).replace(
                    microsecond=0).isoformat()
            log.info(
                'Retaining tasks completed since {0}'.format(completed_since))
        else:
            completed_since = 'now'
        tasks_params['completed_since'] = completed_since

        tasks_json = asana.get(
            'project_tasks', {'project_id': self.id}, expand='.',
            params=tasks_params)
        return tasks_json

    @staticmethod
    def create_project(
            asana, project_id, current_time_utc, task_filters=None,
            section_filters=None, completed_lookback_hours=None, include_subtasks=True):
        '''Creates a Project utilizing data from Asana.

        Using filters, a project attempts to optimize the calls it makes to
        Asana's API. After the JSON data has been collected, it is then parsed
        into Task and Section objects, and then filtered again in order to
        perform filtering that is only possible post-parsing.

        :param asana: The initialized Asana object that makes API calls
        :param project_id: The Asana Project ID
        :param task_filters: A list of tag filters for filtering out tasks
        :param section_filters: A list of sections to filter out tasks
        :param completed_lookback_hours: An amount in hours to look back for
        completed tasks
        :return: The newly created Project instance
        '''
        log.info('Creating project object from Asana Project {0}'.format(
            project_id))

        project_json = asana.get('project', {'project_id': project_id})
        project = Project(project_id, project_json[u'name'], project_json[u'notes'])

        # FIXME: this next section could be refactored out since it is essentially
        # identical to steps in populating task subtasks

        project_tasks_json = project.get_tasks_json(asana, current_time_utc, completed_lookback_hours)
        project.populate_tasks_from_json(asana, project_tasks_json, current_time_utc, task_filters, section_filters, completed_lookback_hours, include_subtasks) 
                
        return project


class Section(object):
    '''A class representing a section of tasks within an Asana Project.'''

    def __init__(self, name, tasks=None):
        self.name = name
        self.tasks = tasks
        if self.tasks is None:
            self.tasks = []

    @staticmethod
    def create_sections(project_tasks_json, task_comments):
        '''Creates sections from task and story JSON from Asana's API.

        :param project_tasks_json: The JSON object for a Project's tasks in
        Asana
        :param task_last_comments: The last comments (stories) for all of the
        tasks in the tasks JSON
        '''
        sections = []
        misc_section = Section(u'Misc:')
        current_section = misc_section
        for task in project_tasks_json:
            if task[u'name'].endswith(':'):
                if current_section.tasks and current_section.name != u'Misc:':
                    sections.append(current_section)
                current_section = Section(task[u'name'])
            else:
                name = task[u'name']
                if (u'assignee' in task) and (task[u'assignee']):
                    assignee = task[u'assignee'][u'name']
                else:
                    assignee = None
                task_id = unicode(task[u'id'])
                if (u'competed' in task):
                    completed = task[u'completed']
                else:
                    completed = False
                if completed:
                    completion_time = dateutil.parser.parse(
                        task[u'completed_at'])
                else:
                    completion_time = None
                description = task[u'notes'] if ((u'notes' in task) and task[u'notes']) else None
                due_date = task[u'due_on'] if ((u'due_on' in task) and task[u'due_on']) else None
                if (u'tags' in task and task[u'tags']):
                    tags = [tag[u'name'] for tag in task[u'tags']]
                else:
                    tags = None
                current_task_comments = task_comments.get(task_id)
                current_task = Task(
                    task_id, name, assignee, completed, completion_time, description,
                    due_date, tags, current_task_comments)
                current_section.add_task(current_task)
        if current_section.tasks:
            sections.append(current_section)
        if misc_section.tasks and current_section != misc_section:
            log.info("Some tasks weren't in a section, adding Misc Section")
            sections.append(misc_section)
        return sections

    def add_task(self, task):
        '''Add a task to a Section's list of tasks.

        :param task: The task to add to the Section's list of tasks
        '''
        if isinstance(task, Task):
            self.tasks.append(task)

    def add_tasks(self, tasks):
        '''Extend the Section's list of tasks with a new list of tasks.

        :param tasks: The list of tasks to extend the Section's list of tasks.
        '''
        self.tasks.extend((task for task in tasks if isinstance(task, Task)))


class Task(TaskContainer):
    '''A class representing an Asana Task.'''

    def __init__(
            self, task_id, name, assignee, completed, completion_time, description,
            due_date, tags, comments, sections = None):
        super(Task, self).__init__(task_id, name, description, sections)
        self.description = description
        self.assignee = assignee
        self.completed = completed
        self.completion_time = completion_time
        self.due_date = due_date
        self.tags = tags
        self.comments = comments
        if sections:
            self.sections = sections
        else:
            self.sections = []

    def tags_in(self, tag_filter_set):
        '''Determines if a Tasks's tags are within a set of tag filters'''
        task_tag_set = frozenset(self.tags)
        return task_tag_set >= tag_filter_set

    def get_tasks_json(self, asana, current_time_utc, completed_lookback_hours):
 
        tasks_params = {}
        if completed_lookback_hours:
            print current_time_utc
            print type(current_time_utc)
            completed_since = (current_time_utc - datetime.timedelta(
                hours=completed_lookback_hours)).replace(
                    microsecond=0).isoformat()
            log.info(
                'Retaining tasks completed since {0}'.format(completed_since))
        else:
            completed_since = 'now'

        tasks_params['completed_since'] = completed_since

        subtasks_json = asana.get('task_subtasks', {'task_id': self.id}, expand='.', params=tasks_params)
        return subtasks_json


# Filters

def last_comment(task_comments):
    if task_comments:
        return task_comments[-1:]
    else:
        return []


def most_recent_comments(task_comments, num_comments):
    if num_comments <= 0:
        num_comments = 1
    elif num_comments > len(task_comments):
        num_comments = len(task_comments)
    if task_comments:
        return task_comments[-num_comments:]
    else:
        return []


def comments_within_lookback(task_comments, current_time_utc, hours):
    filtered_comments = []
    for comment in task_comments:
        comment_time = dateutil.parser.parse(comment[u'created_at'])
        delta = current_time_utc - comment_time
        if delta < datetime.timedelta(hours=hours):
            filtered_comments.append(comment)
    if not filtered_comments and task_comments:
        filtered_comments.append(task_comments[-1])
    return filtered_comments


def as_date(datetime_str):
    try:
        parsed_date = dateutil.parser.parse(datetime_str).date().isoformat()
    except:
        return datetime_str
    else:
        return parsed_date


def generate_templates(
        projects, html_template, text_template, current_date, current_time_utc, skip_inline_css=False, templates_dir="templates"):
    '''Generates the templates using Jinja2 templates

    :param html_template: The filename of the HTML template in the templates
    folder
    :param text_template: The filename of the text template in the templates
    folder
    :param current_date: The current date.
    '''
    env = Environment(
        loader=FileSystemLoader(templates_dir), trim_blocks=True,
        lstrip_blocks=True, autoescape=True)

    env.filters['last_comment'] = last_comment
    env.filters['most_recent_comments'] = most_recent_comments
    env.filters['comments_within_lookback'] = comments_within_lookback
    env.filters['as_date'] = as_date

    log.info('Rendering HTML Template')
    html = env.get_template(html_template)
    if skip_inline_css:
        rendered_html = html.render(
            projects=projects, current_date=current_date,
            current_time_utc=current_time_utc)
    else:
        rendered_html = premailer.transform(html.render(
            projects=projects, current_date=current_date,
            current_time_utc=current_time_utc))

    log.info('Rendering Text Template')
    env.autoescape = False
    plaintext = env.get_template(text_template)
    rendered_plaintext = plaintext.render(
        projects=projects, current_date=current_date,
        current_time_utc=current_time_utc)

    return (rendered_html, rendered_plaintext)


def send_email(
        projects, mail_server, from_address, to_addresses, cc_addresses,
        rendered_html, rendered_text, current_date, smtp_username=None, smtp_password=None, smtp_port=None):
    '''Sends an email using a Project and rendered templates.

    :param projects: The list of Project instances for this email
    :param mail_server: The hostname of the SMTP server to send mail from
    :param from_address: The From: Address for the email to send
    :param to_addresses: The list of To: addresses for the email to be sent to
    :param cc_addresses: The list of Cc: addresses for the email to be sent to
    :param rendered_html: The rendered HTML template
    :param rendered_text: The rendered text template
    :param current_date: The current date
    :param smtp_username: The username to authenticate to SMTP server with
    :param smtp_password: The password to authenticate to SMTP server with
    :param smtp_port: The port to connect to the SMTP server with
    '''

    to_address_str = ', '.join(to_addresses)
    if cc_addresses:
        cc_address_str = ', '.join(cc_addresses)
    else:
        cc_address_str = ''

    log.info('Preparing Email - From: ({0}) To: ({1}) Cc: ({2})'.format(
        from_address, to_address_str, cc_address_str))
    message = MIMEMultipart('alternative')
    message['Subject'] = '{0} Daily Mailer {1}'.format(
        projects[0].name, current_date)
    message['From'] = from_address
    message['To'] = to_address_str
    if cc_addresses:
        message['Cc'] = cc_address_str

    text_part = MIMEText(rendered_text.encode('utf-8'), 'plain')
    html_part = MIMEText(rendered_html.encode('utf-8'), 'html')

    message.attach(text_part)
    message.attach(html_part)

    if cc_addresses:
        to_addresses.extend(cc_addresses)

    try:
        if (smtp_username != None and smtp_password != None):
            if not smtp_port:
                smtp_port = 465
            log.info('Connecting to authenticated SMTP Server: {0}'.format(mail_server))
            smtp_conn = smtplib.SMTP_SSL(mail_server, port=smtp_port, timeout=300)
            log.info('Logging in to Email')
            smtp_conn.ehlo()
            smtp_conn.login(smtp_username, smtp_password)
        else:
            log.info('Connecting to anonymous SMTP Server: {0}'.format(mail_server))
            smtp_conn = smtplib.SMTP(mail_server, timeout=300)
            log.info('Sending Email')
        smtp_conn.sendmail(from_address, to_addresses, message.as_string())
        smtp_conn.quit()
    except smtplib.SMTPException:
        log.exception('Email could not be sent!')


def write_rendered_files(rendered_html, rendered_text, current_date):
    '''Writes the rendered files out to disk.

    Currently, this creates a AsanaMailer_[Date].html and *.markdown file.

    :param rendered_html: The rendered HTML template.
    :param rendered_text: The rendered text template.
    :param current_date: The current date.
    '''
    with codecs.open(
            'AsanaMailer_{0}.html'.format(current_date), 'w', 'utf-8') as (
            html_file):
        log.info('Writing HTML File')
        html_file.write(rendered_html)
    with codecs.open(
            'AsanaMailer_{0}.markdown'.format(current_date), 'w', 'utf-8') as (
            markdown_file):
        log.info('Writing Text File')
        markdown_file.write(rendered_text)


def create_cli_parser():
    parser = argparse.ArgumentParser(
        description='Generates an email template for an Asana project',
        fromfile_prefix_chars='@')
    parser.add_argument('project_id', help='the asana project id')
    parser.add_argument('api_key', help='your asana api key')
    parser.add_argument(
        '-i', '--skip-inline-css',
        action='store_false', 
        default=True, help='skip inlining of CSS in rendered HTML')
    parser.add_argument(
        '-c', '--completed', type=int, dest='completed_lookback_hours',
        metavar='HOURS',
        help='show non-archived tasks completed within the past hours '
        'specified')
    parser.add_argument(
        '-f', '--filter-tags', nargs='+', dest='tag_filters', default=[],
        metavar='TAG', help='tags to filter tasks on')
    parser.add_argument(
        '-s', '--filter-sections', nargs='+', dest='section_filters',
        default=[], metavar='SECTION', help='sections to filter tasks on')
    parser.add_argument(
        '--templates-dir', default='templates',
        help='path to directory with html and plaintext templates')
    parser.add_argument(
        '--html-template', default='Default.html',
        help='a custom template to use for the html portion')
    parser.add_argument(
        '--text-template', default='Default.markdown',
        help='a custom template to use for the plaintext portion')
    email_group = parser.add_argument_group(
        'email', 'arguments for sending emails')
    email_group.add_argument(
        '--mail-server', metavar='HOSTNAME', default='localhost',
        help='the hostname of the mail server to send email from '
        '(default: localhost)')
    email_group.add_argument(
        '--to-addresses', nargs='+', metavar='ADDRESS',
        help="the 'To:' addresses for the outgoing email")
    email_group.add_argument(
        '--cc-addresses', nargs='+', metavar='ADDRESS',
        help="the 'Cc:' addresses for the outgoing email")
    email_group.add_argument(
        '--from-address', metavar='ADDRESS',
        help="the 'From:' address for the outgoing email")
    email_group.add_argument(
        '--username', metavar='ADDRESS', default=None,
        help="the username to authenticate to the outgoing (SMTP) mail server over SSL")
    email_group.add_argument(
        '--password', metavar='ADDRESS', default=None,
        help="the password to authenticate to the outgoing (SMTP) mail server over SSL")

    return parser


def main():
    '''The main function for generating the mailer.

    Based on the arguments, the mailer generates a Project object with its
    appropriate Section and Tasks objects, and then renders templates
    accordingly. This can either be written out to two files, or can be mailed
    out using a SMTP server running on localhost.
    '''

    parser = create_cli_parser()
    args = parser.parse_args()

    if bool(args.from_address) != bool(args.to_addresses):
        parser.error(
            "'To:' and 'From:' address are required for sending email")

    asana = AsanaAPI(args.api_key)
    filters = frozenset((unicode(filter) for filter in args.tag_filters))
    section_filters = frozenset(
        (unicode(section + ':') for section in args.section_filters))
    current_time_utc = datetime.datetime.now(dateutil.tz.tzutc())
    current_date = str(datetime.date.today())
    projects = []
    for project_id in string.split(args.project_id, ","):
        print "id: [%s]" % (project_id)
        project = Project.create_project(
            asana, string.strip(project_id), current_time_utc, task_filters=filters,
            section_filters=section_filters,
            completed_lookback_hours=args.completed_lookback_hours)
        projects.append(project)
    rendered_html, rendered_text = generate_templates(
        projects, args.html_template, args.text_template, current_date,
        current_time_utc, args.skip_inline_css, args.templates_dir)

    if args.to_addresses and args.from_address:
        if args.cc_addresses:
            cc_addresses = args.cc_addresses[:]
        else:
            cc_addresses = None
        send_email(
            projects, args.mail_server, args.from_address, args.to_addresses[:],
            cc_addresses, rendered_html, rendered_text, current_date, args.username, args.password)
    else:
        write_rendered_files(rendered_html, rendered_text, current_date)
    log.info('Finished')


if __name__ == '__main__':
    main()
