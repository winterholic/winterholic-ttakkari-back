from ttakkari.kb_delivery import document_title

GOAL = "디자인 시스템·UI/UX 지식 백과사전 구축"


def test_page_title_wins_when_it_describes_the_page():
    assert document_title("Carbon Buttons", GOAL, "https://x.dev/b", "text") == "Carbon Buttons"


def test_mission_goal_title_is_replaced_by_the_first_heading():
    assert document_title(None, GOAL, "https://raw.githubusercontent.com/o/r/sha/README.md", "# Badge API\nbody") == "Badge API"
    assert document_title(GOAL, GOAL, "https://x.dev", "# Real Title\n") == "Real Title"


def test_raw_repository_file_without_heading_uses_repo_and_path():
    url = "https://raw.githubusercontent.com/twilio-labs/paste/abc123/packages/core/CHANGELOG.md"
    assert document_title(None, GOAL, url, "export const meta = {}") == "paste / packages/core/CHANGELOG.md"


def test_generic_site_title_falls_back_to_frontmatter():
    text = "---\ntitle: 'Menu'\n---\nbody"
    assert document_title("Nextra: the next docs builder", GOAL, "https://t.im/menu", text) == "Menu"
