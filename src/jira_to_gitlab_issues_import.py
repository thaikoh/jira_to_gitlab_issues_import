import requests
from requests.auth import HTTPBasicAuth
import re
import uuid
import os
from gitlab import Gitlab, exceptions as gitlabexceptions
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List
import logging
from io import BytesIO
import time


class ImportConfig:
    # Jira project key that will be imported
    _JIRA_PROJECT = "KEY"
    _JIRA_URL = "https://yourdomain.atlassian.net/"
    # Jira username and private access key
    _JIRA_ACCOUNT = ("user@yourdomain.com", "private_access_key")
    _JIRA_VERIFY_SSL = True
    # If Jira has sprints or milestones feature this variable stores name of the field
    _JIRA_MILESTONE_FIELD = "customfield_10000"
    # Jira issue types that will be imported as Incidens in GitLab, otherwise Issue
    _JIRA_INCIDENT_TYPES = ("bug", "Bug")
    _JIRA_DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%f%z"
    _JIRA_DATE_FORMAT = "%Y-%m-%d"

    # ID of detination GitLab project
    _GITLAB_PROJECT = 123
    _GITLAB_URL = "https://gitlab.yourdomain.com/"
    # Personal access token of user with role Admin
    _GITLAB_TOKEN = "gitlab_token"
    _GITLAB_HEADERS = {"PRIVATE-TOKEN": _GITLAB_TOKEN}
    _GITLAB_DEFAULT_USER = "root"
    _GITLAB_SUDO = True
    _GITLAB_VERIFY_SSL = True
    _GITLAB_PREMIUM = False

    # Map users [Jira diplay name]: [GitLab login]
    # All users must be a memebers of the GitLab project with Reporter role at least
    _USER_NAME_MAP = {
        "Display Name": "login",
        "Display2 Name2": "login2"
    }

    # Map Jira issue type to GitLab issue type
    _ISSUE_TYPE_MAP = {
        "Bug": "bug",
        "Improvement": "improvement",
        "Spike": "spike",
        "Story": "story",
        "story": "story",
        "Task": "task",
        "Subtask": "subtask",
        "Epic": "epic",
        "epic": "epic"
    }

    # Media files extensions
    _MEDIA_EXT = ("jpeg", "jpg", "bmp", "png", "gif", "svg", "mp4", "mpeg", "mov", "avi", "mkv")


@dataclass
class JiraUser:
    display_name: str
    account_id: str


@dataclass
class JiraAttachment:
    author: JiraUser
    filename: str
    content: BytesIO


@dataclass
class JiraComment:
    author: JiraUser
    body: str
    created: datetime

    def __init__(self, **kwargs):
        self.author = kwargs["author"]
        self.body = kwargs["body"]
        self.created = datetime.strptime(kwargs["created"], "%Y-%m-%dT%H:%M:%S.%f%z")


@dataclass
class JiraMilestone:
    id: int
    name: str
    state: str
    start_date: datetime
    end_date: datetime

    def __init__(self, **kwargs):
        self.id = kwargs["id_"]
        self.name = kwargs["name"]
        self.state = kwargs["state"]
        self.start_date = kwargs["start_date"]
        self.end_date = kwargs["end_date"]


@dataclass
class JiraIssue(ImportConfig):
    id: int
    self_: str
    key: str
    created: datetime
    updated: datetime
    summary: str
    reporter: JiraUser
    assignee: Optional[JiraUser]
    time_spent: int
    time_estimate: int
    type: str
    priority: str
    status: str
    description: str = ' '
    due_date: datetime = None
    labels: [str] = None
    parent: int = None
    inward: [int] = field(default_factory=lambda: [])
    outward: [int] = field(default_factory=lambda: [])
    attachments: [JiraAttachment] = field(default_factory=lambda: [])
    comments: [JiraComment] = field(default_factory=lambda: [])
    milestones: [JiraMilestone] = field(default_factory=lambda: [])

    def __init__(self, issue):
        self.id = int(issue["id"])
        self.self_ = issue["self"]
        self.key = issue["key"]
        self.created = datetime.strptime(issue["fields"]["created"], self._JIRA_DATETIME_FORMAT)
        self.updated = datetime.strptime(issue["fields"]["updated"], self._JIRA_DATETIME_FORMAT)
        if issue["fields"]["duedate"]:
            self.due_date = datetime.strptime(issue["fields"]["duedate"], self._JIRA_DATE_FORMAT)
        self.summary = issue["fields"]["summary"]
        self.description = issue["fields"]["description"]
        self.reporter = JiraUser(display_name=issue["fields"]["reporter"]["displayName"],
                                 account_id=issue["fields"]["reporter"]["accountId"])
        if issue["fields"]["assignee"]:
            self.assignee = JiraUser(display_name=issue["fields"]["assignee"]["displayName"],
                                     account_id=issue["fields"]["assignee"]["accountId"])
        else:
            self.assignee = None
        self.time_spent = issue["fields"]["timespent"]
        self.time_estimate = issue["fields"]["timeoriginalestimate"]
        self.type = issue["fields"]["issuetype"]["name"]
        self.priority = issue["fields"]["priority"]["name"]
        self.labels = issue["fields"]["labels"]
        milestones = []
        if issue["fields"][self._JIRA_MILESTONE_FIELD]:
            for milestone in issue["fields"][self._JIRA_MILESTONE_FIELD]:
                start_date = None
                if "startDate" in milestone.keys():
                    start_date = datetime.strptime(milestone["startDate"], self._JIRA_DATETIME_FORMAT)
                end_date = None
                if "endDate" in milestone.keys():
                    end_date = datetime.strptime(milestone["endDate"], self._JIRA_DATETIME_FORMAT)
                milestones.append(JiraMilestone(id_=milestone["id"], name=milestone["name"], state=milestone["state"],
                                                start_date=start_date, end_date=end_date))
        self.milestones = milestones
        self.status = issue["fields"]["status"]["name"]
        if "parent" in issue["fields"]:
            self.parent = int(issue["fields"]["parent"]["id"])
        self.inward = []
        self.outward = []
        for link in issue["fields"]["issuelinks"]:
            if "inwardIssue" in link:
                self.inward.append(int(link["inwardIssue"]["id"]))
            if "outwardIssue" in link:
                self.outward.append(int(link["outwardIssue"]["id"]))


class Jira(ImportConfig):
    __jira_users: List[JiraUser]
    __jira_issues: List[JiraIssue]
    __jira_issue_index: int

    @property
    def _jira_issues_count(self) -> int:
        if self.__jira_issues:
            return len(self.__jira_issues)
        else:
            return 0

    @property
    def _jira_users(self):
        return self.__jira_users

    def __init__(self):
        logging.info(f"Jira project key {self._JIRA_PROJECT}")
        self.__jira_users = self.__retrieve_jira_users()
        if self.__jira_users:
            logging.info(f"Retrieved {len(self.__jira_users)} Jira users")
        self.__jira_issues = self.__retrieve_jira_issues_list()
        if self.__jira_issues:
            logging.info(f"Retrieved {len(self.__jira_issues)} Jira issues")

    def __retrieve_jira_issues_list(self) -> Optional[List[JiraIssue]]:
        self._reset_issue_index()
        issues = []
        start_at = 0
        max_results = 100
        while True:
            jira_issues = requests.get(
                f'{self._JIRA_URL}rest/api/2/search?jql=project={self._JIRA_PROJECT}+ORDER+BY+id+ASC&maxResults='
                f'{str(max_results)}&startAt={str(start_at)}',
                auth=HTTPBasicAuth(*self._JIRA_ACCOUNT),
                verify=self._JIRA_VERIFY_SSL,
                headers={"Content-Type": "application/json"}
            ).json()
            if "issues" in jira_issues.keys():
                issues = issues + jira_issues["issues"]
                start_at = start_at + max_results
                if start_at > jira_issues["total"]:
                    break
            else:
                break
        if len(issues):
            return [JiraIssue(issue) for issue in issues]

    def __retrieve_jira_users(self) -> Optional[List[JiraUser]]:
        jira_users = requests.get(
            f'{self._JIRA_URL}rest/api/2/user/assignable/search?project={self._JIRA_PROJECT}',
            auth=HTTPBasicAuth(*self._JIRA_ACCOUNT),
            verify=self._JIRA_VERIFY_SSL,
            headers={"Content-Type": "application/json"}
        ).json()
        if len(jira_users) > 0 and "errorMessages" not in jira_users:
            return [JiraUser(display_name=user["displayName"], account_id=user["accountId"]) for user in jira_users]
        else:
            logging.error(f"Can't retrieve Jira users, error {jira_users['errorMessages']}")
            return None

    def _find_jira_user(self, **kwargs) -> Optional[JiraUser]:
        for user in self.__jira_users:
            if ("account_id" in kwargs.keys() and user.account_id == kwargs["account_id"]) or \
                    ("display_name" in kwargs.keys() and user.display_name == kwargs["display_name"]):
                return user
        return None

    def _find_jira_issue(self, id_: int) -> Optional[JiraIssue]:
        for issue in self.__jira_issues:
            if id_ == issue.id:
                return self.__retrieve_attachments_and_comments(issue)
        return None

    def _next_jira_issue(self) -> Optional[JiraIssue]:
        if len(self.__jira_issues) > self.__jira_issue_index:
            issue = self.__retrieve_attachments_and_comments(self.__jira_issues[self.__jira_issue_index])
            self.__jira_issue_index += 1
            return issue
        else:
            return None

    def _reset_issue_index(self):
        self.__jira_issue_index = 0

    def __retrieve_attachments_and_comments(self, issue: JiraIssue) -> JiraIssue:
        issue_details = requests.get(
            issue.self_,
            auth=HTTPBasicAuth(*self._JIRA_ACCOUNT),
            verify=self._JIRA_VERIFY_SSL,
            headers={"Content-Type": "application/json"}
        ).json()
        attachments = []
        for attachment in issue_details["fields"]["attachment"]:
            file = requests.get(
                attachment["content"],
                auth=HTTPBasicAuth(*self._JIRA_ACCOUNT),
                verify=self._JIRA_VERIFY_SSL
            )
            attachments.append(JiraAttachment(
                author=JiraUser(display_name=attachment["author"]["displayName"],
                                account_id=attachment["author"]["accountId"]),
                filename=attachment["filename"], content=BytesIO(file.content)))
        issue.attachments = attachments
        comments = []
        for comment in issue_details["fields"]["comment"]["comments"]:
            comments.append(
                JiraComment(
                    author=JiraUser(display_name=comment["author"]["displayName"],
                                    account_id=comment["author"]["accountId"]),
                    body=comment["body"],
                    created=comment["created"]))
        issue.comments = comments
        return issue


@dataclass
class GitLabUser:
    id: int
    login: str

    def __init__(self, **kwargs):
        self.id = kwargs["id_"]
        self.login = kwargs["login"]


@dataclass
class GitLabIssue:
    assignee_id: int = None
    created_at: str = None
    description: str = None
    epic_id: int = None
    id: int = None
    issue_type: str = None
    labels: str = None
    milestone_id: int = None
    title: str = None
    weight: int = None
    due_date: str = None


class GitLabImport(Jira):
    __gitlab: Gitlab
    __project = None
    __gitlab_users = [GitLabUser]
    __gitlab_default_user = GitLabUser
    __gitlab_headers = None
    __replace_user_dict = {}
    __jira_gitlab_issues_hash = {}
    __jira_gitlab_milestone_hash = {}

    def __init__(self):
        super().__init__()
        logging.info(f"GitLab project id {self._GITLAB_PROJECT}")
        self.__gitlab = Gitlab(self._GITLAB_URL, private_token=self._GITLAB_TOKEN, ssl_verify=self._GITLAB_VERIFY_SSL)
        self.__gitlab.auth()
        try:
            self.__project = self.__gitlab.projects.get(self._GITLAB_PROJECT)
        except (gitlabexceptions.GitlabHttpError, gitlabexceptions.GitlabGetError) as e:
            logging.error(f"Exception {e}")
        self.__gitlab_users = self.__retrieve_gitlab_users()
        logging.info(f"Retrieved {len(self.__gitlab_users)} GitLab users")
        if self._jira_users:
            for jira_user in self._jira_users:
                if not self.__map_jira_user(display_name=jira_user.display_name):
                    logging.warning(f"User {jira_user.display_name} not found in GitLab")
                    answer = input("Continue? [y/n]")
                    if answer.lower() not in ["y", "yes"]:
                        quit()
            self.__replace_user_dict = self.__jira_users_replace_dict()

    def __jira_users_replace_dict(self) -> Optional[dict]:
        if not len(self._jira_users) or not len(self.__gitlab_users):
            return None
        replace_dict = {}
        for user in self._jira_users:
            key = r"\[~accountid:"+user.account_id+"\]"
            value = r"@"+self.__map_jira_user(display_name=user.display_name).login
            replace_dict[key] = value
        return replace_dict

    def __retrieve_gitlab_users(self) -> list:
        if self.__project is None:
            return []
        self.__gitlab_headers = self._GITLAB_HEADERS
        members = self.__project.users.list()
        for member in members:
            if self._GITLAB_DEFAULT_USER == member.attributes.get("username"):
                self.__gitlab_default_user = GitLabUser(id_=member.id, login=member.attributes.get("username"))
                self.__gitlab_headers["SUDO"] = self.__gitlab_default_user.login
                continue
        return [GitLabUser(id_=member.id, login=member.attributes.get("username")) for member in members]

    def __map_jira_user(self, **kwargs) -> Optional[GitLabUser]:
        if "display_name" not in kwargs.keys():
            if "account_id" in kwargs.keys():
                kwargs["display_name"] = self._find_jira_user(account_id=kwargs["account_id"]).display_name
            else:
                return self.__gitlab_default_user
        for gitlab_user in self.__gitlab_users:
            if kwargs["display_name"] in self._USER_NAME_MAP.keys():
                if gitlab_user.login == self._USER_NAME_MAP[kwargs["display_name"]]:
                    return gitlab_user
        return self.__gitlab_default_user

    def __multiple_replace(self, text: str, dictionary=None) -> Optional[str]:
        if dictionary is None:
            dictionary = {}
        if text is None:
            return ''
        t = text

        t = re.sub(r'\s*\{noformat}\s*', r'```', t)
        #t = re.sub(r'\n\n\*(![\n\*])\*\n\n{{ "": "(.+)"}}', r'<details>\n<summary>\1</summary>\n\n\2</details>\n', t)

        t = re.sub(r'(\r\n){1}', r'  \1', t)  # line breaks
        t = re.sub(r'\{code:([a-z]+)}\s*', r'\n```\1\n', t)  # Block code
        t = re.sub(r'\{code}\s*', r'\n```\n', t)  # Block code
        t = re.sub(r'\n\s*bq\. (.*)\n', r'\n\> \1\n', t)  # Block quote
        t = re.sub(r'\{quote}', r'\n\>\>\>\n', t)  # Block quote #2
        t = re.sub(r'\{color:[#\w]+}(.*)\{color}', r'> **\1**', t)  # Colors
        t = re.sub(r'\n-{4,}\n', r'---', t)  # Ruler
        #t = re.sub(r'\[~([a-z]+)\]', r'@\1', t)  # Links to users
        t = re.sub(r'\[([^~|\]]*)]', r'\1', t)  # Links without alt
        t = re.sub(r'\[(?:(.+)\|)([a-z]+://.+)]', r'[\1](\2)', t)  # Links with alt
        #t = re.sub(r'(\b%s-\d+\b)' % self._JIRA_PROJECT,
        #           r'[\1](%sbrowse/\1)' % self._JIRA_URL, t)
        # Lists
        t = re.sub(r'\n *# ', r'\n1. ', t)  # Ordered list
        t = re.sub(r'\n *[*\-#]# ', r'\n   1. ', t)  # Ordered sub-list
        t = re.sub(r'\n *[*\-#]{2}# ', r'\n      1. ', t)  # Ordered sub-sub-list
        t = re.sub(r'\n *[*\-#]{3}# ', r'\n         1. ', t)  # Ordered sub-sub-list
        t = re.sub(r'\n *\* ', r'\n - ', t)  # Unordered list
        t = re.sub(r'\n *[*\-#][*\-] ', r'\n   - ', t)  # Unordered sub-list
        # Unordered sub-sub-list
        t = re.sub(r'\n *[*\-#]{2}[*\-] ', r'\n     - ', t)
        # Text effects
        t = re.sub(r'(^|[\W])\*(\S.*\S)\*([\W]|$)', r'\1**\2**\3', t)  # Bold
        t = re.sub(r'(^|[\W])_(\S.*\S)_([\W]|$)', r'\1*\2*\3', t)  # Emphasis
        # Deleted / Strikethrough
        t = re.sub(r'(^|[\W])-(\S.*\S)-([\W]|$)', r'\1~~\2~~\3', t)
        t = re.sub(r'(^|[\W])\+(\S.*\S)\+([\W]|$)', r'\1__\2__\3', t)  # Underline
        t = re.sub(r'(^|[\W])\{\{(.*)}}([\W]|$)', r'\1`\2`\3', t)  # Inline code
        # Titles
        t = re.sub(r'[\n^]h1\. ', r'\n# ', t)
        t = re.sub(r'[\n^]h2\. ', r'\n## ', t)
        t = re.sub(r'[\n^]h3\. ', r'\n### ', t)
        t = re.sub(r'[\n^]h4\. ', r'\n#### ', t)
        t = re.sub(r'[\n^]h5\. ', r'\n##### ', t)
        t = re.sub(r'[\n^]h6\. ', r'\n###### ', t)
        # Emojis : https://emoji.codes
        t = re.sub(r':\)', r':smiley:', t)
        t = re.sub(r':\(', r':disappointed:', t)
        t = re.sub(r':P', r':yum:', t)
        t = re.sub(r':D', r':grin:', t)
        t = re.sub(r';\)', r':wink:', t)
        t = re.sub(r'\(y\)', r':thumbsup:', t)
        t = re.sub(r'\(n\)', r':thumbsdown:', t)
        t = re.sub(r'\(i\)', r':information_source:', t)
        t = re.sub(r'\(/\)', r':white_check_mark:', t)
        t = re.sub(r'\(x\)', r':x:', t)
        t = re.sub(r'\(!\)', r':warning:', t)
        t = re.sub(r'\(\+\)', r':heavy_plus_sign:', t)
        t = re.sub(r'\(-\)', r':heavy_minus_sign:', t)
        t = re.sub(r'\(\?\)', r':grey_question:', t)
        t = re.sub(r'\(on\)', r':bulb:', t)
        # t = re.sub(r'\(off\)', r'::', t) # Not found
        t = re.sub(r'\(\*[rgby]?\)', r':star:', t)

        if dictionary:
            for k, v in dictionary.items():
                t = re.sub(k, v, t)

        return t

    def __upload_attachment(self, attachment: JiraAttachment) -> Optional[dict]:
        if self._GITLAB_SUDO:
            self.__gitlab_headers["SUDO"] = self.__map_jira_user(display_name=attachment.author.display_name).login
        fn, extension = os.path.splitext(attachment.filename)
        extension = extension.lower()
        filename = str(uuid.uuid4()) + extension
        request = requests.post(
            f"{self._GITLAB_URL}api/v4/projects/{self._GITLAB_PROJECT}/uploads",
            headers=self.__gitlab_headers,
            files={"file": (filename, attachment.content)},
            verify=self._GITLAB_VERIFY_SSL
        )
        if request.ok:
            file_info = request.json()
            if "url" in file_info:
                key = f"!{attachment.filename}[^!]*!"
                value = f"![{attachment.filename}]({file_info['url']})  \n"
                return {key: value}
        else:
            logging.warning(f"Attachment {attachment.filename} {int(attachment.content.getbuffer().nbytes/1024)}Kb"
                            f" isn't uploaded, error code {request.status_code}, response {request.content}")
        return None

    def __import_issue(self, jira_issue: JiraIssue):
        if jira_issue is None:
            return
        # Check if issue already imported
        if jira_issue.id in self.__jira_gitlab_issues_hash.keys():
            return
        logging.info(f"Importing Jira issue {jira_issue.key} id {jira_issue.id}")
        # Saving issue flag  in the hash for prevent doubles and exit from recursion
        self.__jira_gitlab_issues_hash[jira_issue.id] = 0
        # Recursively creating subtask, linked and parent issues first
        linked_issues = jira_issue.inward + jira_issue.outward
        if jira_issue.parent is not None:
            linked_issues.append(jira_issue.parent)
        for id_ in linked_issues:
            if id_ not in self.__jira_gitlab_issues_hash.keys():
                linked_issue = self._find_jira_issue(id_)
                self.__import_issue(linked_issue)
        # Preparing replacement dict
        replace_dict = self.__replace_user_dict
        # Importing issue attachments
        attachment_description = ""
        for attachment in jira_issue.attachments:
            attachment_replace = self.__upload_attachment(attachment)
            if attachment_replace is not None:
                replace_dict.update(attachment_replace.items())
                for key in attachment_replace.keys():
                    attachment_description += f"{re.sub(r'^!', r'', attachment_replace[key])}  \n"
        # Maping Jira issue to GitLab issue
        gitlab_issue = GitLabIssue()
        gitlab_issue.id = self._GITLAB_PROJECT
        if jira_issue.assignee is not None:
            gitlab_issue.assignee_id = self.__map_jira_user(display_name=jira_issue.assignee.display_name).id
        gitlab_issue.created_at = jira_issue.created.isoformat()
        if jira_issue.due_date is not None:
            gitlab_issue.due_date = jira_issue.due_date.strftime("%Y-%m-%d")
        gitlab_issue.description = self.__multiple_replace(jira_issue.description, replace_dict)
        if attachment_description:
            gitlab_issue.description += f"  \n  \n---  \n<b>Attachments:</b>  \n{attachment_description}"
        gitlab_issue.description += f"  \n  \n---  \n<small>" \
                                    f"Jira link: [{jira_issue.key}]({self._JIRA_URL}browse/{jira_issue.key})  \n" \
                                    f"Created/updated: {jira_issue.created.strftime('%d.%m.%Y %H:%M:%S')}/" \
                                    f"{jira_issue.updated.strftime('%d.%m.%Y %H:%M:%S')}</small>  \n"
        if jira_issue.parent in self.__jira_gitlab_issues_hash.keys():
            if self.__jira_gitlab_issues_hash[jira_issue.parent] > 0:
                gitlab_issue.description += f"<small>Parent issue: {self._GITLAB_URL}" \
                                            f"{self.__project.attributes.get('path_with_namespace')}/-/issues/" \
                                            f"{self.__jira_gitlab_issues_hash[jira_issue.parent]}</small>  \n"
        # Add linked issues to description
        if len(jira_issue.inward) > 0:
            blocked_issues = ""
            for linked in jira_issue.inward:
                if linked in self.__jira_gitlab_issues_hash.keys():
                    if self.__jira_gitlab_issues_hash[linked] > 0:
                        blocked_issues += f"{self._GITLAB_URL}" \
                                          f"{self.__project.attributes.get('path_with_namespace')}/-/issues/" \
                                          f"{self.__jira_gitlab_issues_hash[linked]} "
            if len(blocked_issues) > 0:
                gitlab_issue.description += f"<small>Blocked by: {blocked_issues}</small>  \n"
        if len(jira_issue.outward) > 0:
            subtasks = ""
            for subtask in jira_issue.outward:
                if subtask in self.__jira_gitlab_issues_hash.keys():
                    if self.__jira_gitlab_issues_hash[subtask] > 0:
                        subtasks += f"{self._GITLAB_URL}" \
                                    f"{self.__project.attributes.get('path_with_namespace')}/-/issues/" \
                                    f"{self.__jira_gitlab_issues_hash[subtask]} "
            if len(subtasks) > 0:
                gitlab_issue.description += f"<small>Related to: {subtasks}</small>"
        # Setting issue type
        if jira_issue.type in self._JIRA_INCIDENT_TYPES:
            gitlab_issue.issue_type = "incident"
        else:
            gitlab_issue.issue_type = "issue"
        # Setting issue labels including priority and Jira type
        labels = jira_issue.labels
        if jira_issue.status:
            labels.append(f"status::"+jira_issue.status.lower())
        if jira_issue.priority:
            labels.append(f"priority::"+jira_issue.priority.lower())
        if jira_issue.type:
            if jira_issue.type in self._ISSUE_TYPE_MAP.keys():
                labels.append(f"type::"+self._ISSUE_TYPE_MAP[jira_issue.type])
            else:
                labels.append(f"type::"+jira_issue.type)
        gitlab_issue.labels = ",".join(labels)
        gitlab_issue.title = jira_issue.summary
        gitlab_issue.weight = jira_issue.time_estimate
        # Creating milestones
        for jira_milestone in jira_issue.milestones:
            if jira_milestone.id not in self.__jira_gitlab_milestone_hash.keys():
                gitlab_milestone = self.__create_milestone(jira_milestone)
                self.__jira_gitlab_milestone_hash[jira_milestone.id] = gitlab_milestone.attributes.get("id")
            gitlab_issue.milestone_id = self.__jira_gitlab_milestone_hash[jira_milestone.id]
        # Sudo if allowed
        if self._GITLAB_SUDO:
            self.__gitlab_headers["SUDO"] = self.__map_jira_user(display_name=jira_issue.reporter.display_name).login
        else:
            gitlab_issue.description = f"Issue by " \
                                       f"{self.__map_jira_user(display_name=jira_issue.reporter.display_name).login}" \
                                       f"\n\n" + gitlab_issue.description
        # Creating GitLab issue
        created_issue = self.__project.issues.create({k: v for k, v in gitlab_issue.__dict__.items() if v is not None},
                                                     sudo=self.__gitlab_headers["SUDO"])
        # Writing estimate and spent time
        if jira_issue.time_spent:
            created_issue.add_spent_time(str(jira_issue.time_spent) + "s")
            created_issue.save()
        if jira_issue.time_estimate:
            created_issue.time_estimate(str(jira_issue.time_estimate) + "s")
            created_issue.save()
        # Close issue if its in Done status
        if jira_issue.status == "Done":
            created_issue.state_event = "close"
            created_issue.save()
        # Importing issue comments
        for comment in jira_issue.comments:
            comment_text = ""
            if self._GITLAB_SUDO:
                self.__gitlab_headers["SUDO"] = self.__map_jira_user(display_name=comment.author.display_name).login
            else:
                comment_text = f"Comment by {self.__map_jira_user(display_name=comment.author.display_name).login}\n\n"
            comment_text = comment_text + self.__multiple_replace(comment.body, replace_dict)
            body = {"body": comment_text}
            created_issue.notes.create(body, sudo=self.__gitlab_headers["SUDO"])
        # Deleting attachments for memory saving
        del jira_issue.attachments
        # Saving created issue id in hash
        self.__jira_gitlab_issues_hash[jira_issue.id] = created_issue.get_id()
        logging.info(f"Importing issue {jira_issue.key} completed.")
        return created_issue

    # Creating milestone
    def __create_milestone(self, jira_milestone: JiraMilestone):
        milestones = self.__project.milestones.list(title=jira_milestone.name)
        if len(milestones) > 0:
            return milestones[0]
        data = {"title": jira_milestone.name}
        if jira_milestone.start_date:
            data["start_date"] = jira_milestone.start_date.strftime("%Y%m%d")
        if jira_milestone.end_date:
            data["due_date"] = jira_milestone.end_date.strftime("%Y%m%d")
        milestone = self.__project.milestones.create(data)
        if jira_milestone.state == "closed":
            milestone.state_event = "close"
            milestone.save()
        return milestone

    # Iterate via Jira issues and link them
    def __link_imported_issues(self):
        self._reset_issue_index()
        linked = 0
        while True:
            jira_issue = self._next_jira_issue()
            if jira_issue is None:
                break
            if jira_issue.parent is not None:
                linked += self.__create_link(jira_issue.id, jira_issue.parent, "blocks")
            for dst in jira_issue.inward:
                linked += self.__create_link(jira_issue.id, dst, "is_blocked_by")
            for dst in jira_issue.outward:
                linked += self.__create_link(jira_issue.id, dst, "relates_to")
        return linked

    # Link two issues
    def __create_link(self, jira_src_id: int, jira_dst_id: int, link_type: str = "relates_to"):
        if jira_src_id not in self.__jira_gitlab_issues_hash.keys() or \
                jira_dst_id not in self.__jira_gitlab_issues_hash.keys():
            return 0
        if not self._GITLAB_PREMIUM:
            link_type = "relates_to"
        gitlab_issue = self.__project.issues.get(self.__jira_gitlab_issues_hash[jira_src_id])
        data = {
            "target_project_id": self.__project.attributes.get("id"),
            "target_issue_iid": self.__jira_gitlab_issues_hash[jira_dst_id],
            "link_type": link_type
        }
        try:
            gitlab_issue.links.create(data)
        except gitlabexceptions.GitlabCreateError as e:
            logging.warning(f"Exception {e} src={jira_src_id} dst={jira_dst_id} link_type={link_type}")
        return 1

    # Iterate via Jira issues and import it
    def __import_issues(self) -> int:
        self._reset_issue_index()
        imported = 0
        while True:
            jira_issue = self._next_jira_issue()
            if jira_issue is None:
                break
            self.__import_issue(jira_issue)
            imported += 1
        return imported

    def run_import(self):
        time.sleep(1)
        if self.__project is None:
            return
        if self._jira_issues_count == 0:
            logging.warning(f"Nothing to import, quiting")
            return
        answer = input(f"Import {self._jira_issues_count} issues from Jira project {self._JIRA_PROJECT} to GitLab project "
                       f"{self.__project.attributes.get('name')}, continue? [y/n]")
        if answer.lower() not in ["y", "yes"]:
            return
        logging.info(f"Starting import issues")
        logging.info(f"Imported {self.__import_issues()} issues")
        logging.info(f"Starting linking imported issues")
        logging.info(f"Linked {self.__link_imported_issues()} issues")

    def delete_issues(self):
        if self.__project is None:
            return
        issues = self.__project.issues.list(get_all=True)
        if len(issues) == 0:
            logging.info(f"No issues found in GitLab project {self.__project.attributes.get('name')}")
            return
        answer = input(f"Delete {len(issues)} issues in GitLab project {self.__project.attributes.get('name')}, "
                       f"continue? [y/n]")
        if answer.lower() not in ["y", "yes"]:
            return
        for issue in issues:
            issue.delete()
        milestones = self.__project.milestones.list(get_all=True)
        for milestone in milestones:
            milestone.delete()

    def test(self, id_: int):
        print(self.__import_issue(self._find_jira_issue(id_=id_)))
        print(self.__link_imported_issues())


def main():
    logging.basicConfig(level=logging.INFO)
    gitlab = GitLabImport()
    # Delete all issues and milestones first
    gitlab.delete_issues()
    # Import Jira issues
    gitlab.run_import()


if __name__ == "__main__":
    main()
