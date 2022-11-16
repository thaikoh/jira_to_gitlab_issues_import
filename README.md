# Description
Script does import Jira issues to GitLab.

Imports:

- Issues
- Comments
- Attachments of issues and comments
- Links between issues (parent, blocked by, subtask etc)
- Sprints or milestones

Features:

- Delete all issues form destination GitLab projest (defined by user)
- Map Jira users to Gitlab users and post issues and comments on their behalf
- Supports Jira API v2
- Supports GitLab API v4