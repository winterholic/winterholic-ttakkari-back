from typing import Literal

PmsOperation = Literal[
    "list_projects", "create_project", "get_project", "update_project", "delete_project", "dashboard",
    "list_tasks", "create_task", "update_task", "move_task", "delete_task",
    "list_milestones", "create_milestone", "update_milestone", "delete_milestone",
    "list_notes", "create_note", "update_note", "delete_note",
]
