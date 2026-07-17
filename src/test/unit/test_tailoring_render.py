from pathlib import Path
from unittest.mock import patch

from profile.schema import Education, Profile, ProfileItem, Skill
from tailoring.render import build_render_model, latex_escape, render
from tailoring.schema import TailoredSelection

TEMPLATE_PATH = Path(__file__).resolve().parents[3] / "src" / "tailoring" / "templates" / "cv.tex.jinja"


def _profile(**overrides) -> Profile:
    base = dict(
        name="Jane Candidate",
        email="jane@example.com",
        phone=None,
        location="Singapore",
        links=[],
        education=[
            Education(id="edu_1", institution="Test University", degree="BEng", graduation_date="2026")
        ],
        skills=[Skill(id="python", label="Python", category="Programming Languages")],
        experiences=[
            ProfileItem(
                id="exp_1",
                title="Engineer",
                organization="Acme",
                date_range="2024",
                bullets=["Built a thing."],
                demonstrated_skills=["python"],
            )
        ],
        projects=[
            ProfileItem(
                id="proj_1",
                title="Side project",
                organization="Personal",
                date_range="2023",
                bullets=["Shipped a thing."],
                demonstrated_skills=["python"],
            )
        ],
    )
    base.update(overrides)
    return Profile(**base)


def _selection(profile: Profile, **overrides) -> TailoredSelection:
    base = dict(
        summary="A concise summary.",
        experience_order=["exp_1"],
        experiences=[{"ref_id": "exp_1", "bullets": ["Built a thing with Python."]}],
        project_order=["proj_1"],
        projects=[{"ref_id": "proj_1", "bullets": ["Shipped a thing with Python."]}],
        skill_order=["python"],
    )
    base.update(overrides)
    return TailoredSelection.model_validate(base, context={"profile": profile})


def _render_and_capture_tex(tmp_path, selection, profile) -> str:
    """Runs the real render() pipeline but stubs out the tectonic call, so
    the .tex source it wrote to disk can be inspected without needing a
    LaTeX toolchain installed."""
    with patch("tailoring.render._compile_tex") as mock_compile:
        mock_compile.return_value = tmp_path / "cv.pdf"
        render(selection, profile, template_path=TEMPLATE_PATH, output_dir=tmp_path)
    return (tmp_path / "cv.tex").read_text()


# TC-RENDER-01 — identity fields in the model are byte-identical to the source Profile.
def test_build_render_model_identity_fields_are_byte_identical():
    profile = _profile(links=["https://github.com/example"])
    selection = _selection(profile)
    model = build_render_model(selection, profile)
    assert model["name"] == profile.name
    assert model["email"] == profile.email
    assert model["location"] == profile.location
    assert model["headline"] == profile.headline
    assert model["education"][0]["institution"] == profile.education[0].institution
    assert model["education"][0]["degree"] == profile.education[0].degree
    # links: the url itself is untouched; only `display` is computed.
    assert model["links"][0]["url"] == profile.links[0]
    assert model["links"][0]["display"] == "github.com/example"


# TC-RENDER-02 — every LaTeX special char is escaped.
def test_latex_escape_covers_all_special_chars():
    result = latex_escape(r"& % $ # _ { } ~ ^ \ ")
    assert result == r"\& \% \$ \# \_ \{ \} \textasciitilde{} \textasciicircum{} \textbackslash{} "


# TC-RENDER-03 — an unescaped '&' in a bullet reaches the .tex output as '\&'.
def test_ampersand_in_bullet_is_escaped_in_tex_output(tmp_path):
    profile = _profile()
    selection = _selection(
        profile, experiences=[{"ref_id": "exp_1", "bullets": ["Built A & B."]}]
    )
    tex = _render_and_capture_tex(tmp_path, selection, profile)
    assert "Built A \\& B." in tex
    assert "Built A & B." not in tex


# TC-RENDER-04 — .tex output matches a stored snapshot byte-for-byte.
def test_tex_output_matches_snapshot(tmp_path):
    profile = _profile(links=[], phone=None, projects=[])
    selection = _selection(profile, project_order=[], projects=[])
    tex = _render_and_capture_tex(tmp_path, selection, profile)

    expected = (
        "\\documentclass[letterpaper,11pt]{article}\n"
        "\n"
        "\\usepackage{latexsym}\n"
        "\\usepackage[empty]{fullpage}\n"
        "\\usepackage{titlesec}\n"
        "\\usepackage[usenames,dvipsnames]{color}\n"
        "\\usepackage{verbatim}\n"
        "\\usepackage{enumitem}\n"
        "\\usepackage[hidelinks]{hyperref}\n"
        "\\usepackage{fancyhdr}\n"
        "\\usepackage[english]{babel}\n"
        "\\usepackage{tabularx}\n"
        "\\usepackage{geometry}\n"
        "\n"
        "\\geometry{top=0.5in, bottom=0.5in, left=0.6in, right=0.6in}\n"
        "\n"
        "\\pagestyle{fancy}\n"
        "\\fancyhf{}\n"
        "\\fancyfoot{}\n"
        "\\renewcommand{\\headrulewidth}{0pt}\n"
        "\\renewcommand{\\footrulewidth}{0pt}\n"
        "\n"
        "\\urlstyle{same}\n"
        "\\raggedbottom\n"
        "\\raggedright\n"
        "\\setlength{\\tabcolsep}{0in}\n"
        "\n"
        "% Section formatting\n"
        "\\titleformat{\\section}{\n"
        "  \\vspace{-4pt}\\scshape\\raggedright\\large\n"
        "}{}{0em}{}[\\color{black}\\titlerule \\vspace{-5pt}]\n"
        "\n"
        "% Custom commands\n"
        "\\newcommand{\\resumeItem}[1]{\\item\\small{#1 \\vspace{-2pt}}}\n"
        "\n"
        "\\newcommand{\\resumeSubheading}[4]{\n"
        "  \\vspace{-2pt}\\item\n"
        "  \\begin{tabular*}{0.97\\textwidth}[t]{l@{\\extracolsep{\\fill}}r}\n"
        "    \\textbf{#1} & #2 \\\\\n"
        "    \\textit{\\small#3} & \\textit{\\small #4} \\\\\n"
        "  \\end{tabular*}\\vspace{-7pt}\n"
        "}\n"
        "\n"
        "\\newcommand{\\resumeSubItem}[1]{\\resumeItem{#1}\\vspace{-4pt}}\n"
        "\\renewcommand\\labelitemii{$\\vcenter{\\hbox{\\tiny$\\bullet$}}$}\n"
        "\\newcommand{\\resumeSubHeadingListStart}{\\begin{itemize}[leftmargin=0.15in, label={}]}\n"
        "\\newcommand{\\resumeSubHeadingListEnd}{\\end{itemize}}\n"
        "\\newcommand{\\resumeItemListStart}{\\begin{itemize}}\n"
        "\\newcommand{\\resumeItemListEnd}{\\end{itemize}\\vspace{-5pt}}\n"
        "\n"
        "%-------------------------------------------\n"
        "%%%%%%  RESUME STARTS HERE  %%%%%%%%%%%%\n"
        "\n"
        "\\begin{document}\n"
        "\n"
        "%----------HEADING----------\n"
        "\\begin{center}\n"
        "  {\\Huge \\scshape Jane Candidate} \\\\ \\vspace{4pt}\n"
        "  jane@example.com\\end{center}\n"
        "\n"
        "%-----------EDUCATION-----------\n"
        "\\section{Education}\n"
        "\\resumeSubHeadingListStart\n"
        "  \\resumeSubheading\n"
        "    {Test University}{}\n"
        "    {BEng}{}\n"
        "\\resumeSubHeadingListEnd\n"
        "\n"
        "%-----------SKILLS-----------\n"
        "\\section{Skills}\n"
        "\\begin{itemize}[leftmargin=0.15in, label={}]\n"
        "  \\small{\\item{\n"
        "    \\textbf{Programming Languages:} Python \\\\\n"
        "  }}\n"
        "\\end{itemize}\n"
        "\n"
        "%-----------PROJECTS-----------\n"
        "\n"
        "%-----------EXPERIENCE-----------\n"
        "\\section{Experience}\n"
        "\\resumeSubHeadingListStart\n"
        "  \\resumeSubheading\n"
        "    {Engineer}{2024}\n"
        "    {Acme}{}\n"
        "  \\resumeItemListStart\n"
        "    \\resumeItem{Built a thing with Python.}\n"
        "  \\resumeItemListEnd\n"
        "\\resumeSubHeadingListEnd\n"
        "\n"
        "\\end{document}"
    )
    assert tex == expected


# TC-RENDER-05 — experience/project order is respected.
def test_render_respects_order(tmp_path):
    profile = _profile(
        experiences=[
            ProfileItem(id="exp_a", title="Role A", bullets=["a"], demonstrated_skills=[]),
            ProfileItem(id="exp_b", title="Role B", bullets=["b"], demonstrated_skills=[]),
        ],
    )
    selection = _selection(
        profile,
        experience_order=["exp_a", "exp_b"],
        experiences=[
            {"ref_id": "exp_a", "bullets": ["a"]},
            {"ref_id": "exp_b", "bullets": ["b"]},
        ],
    )
    tex = _render_and_capture_tex(tmp_path, selection, profile)
    assert tex.index("Role A") < tex.index("Role B")
    assert "Side project" in tex  # project C present


# TC-RENDER-06 — Skills and Projects precede Experience; Education is present.
def test_section_ordering_and_presence(tmp_path):
    profile = _profile()
    selection = _selection(profile)
    tex = _render_and_capture_tex(tmp_path, selection, profile)

    skills_pos = tex.index(r"\section{Skills}")
    projects_pos = tex.index(r"\section{Projects}")
    experience_pos = tex.index(r"\section{Experience}")
    education_pos = tex.index(r"\section{Education}")

    assert skills_pos < experience_pos
    assert projects_pos < experience_pos
    assert education_pos > 0


# TC-RENDER-07 — raw LaTeX in a bullet is neutralized, not executed as layout.
def test_raw_latex_in_bullet_is_escaped_not_injected(tmp_path):
    profile = _profile()
    selection = _selection(
        profile, experiences=[{"ref_id": "exp_1", "bullets": [r"\textbf{foo}"]}]
    )
    tex = _render_and_capture_tex(tmp_path, selection, profile)
    assert r"\textbf{foo}" not in tex
    assert r"\textbackslash{}textbf\{foo\}" in tex


# Bold-marker convention: the LLM's `**term**` becomes \textbf{term}, and
# raw LaTeX inside a bold span is still neutralized (the marker only ever
# grants emphasis, never an escape hatch into arbitrary LaTeX).
def test_bold_marker_becomes_textbf(tmp_path):
    profile = _profile()
    selection = _selection(
        profile,
        experiences=[{"ref_id": "exp_1", "bullets": ["Built it with **Python** and speed."]}],
    )
    tex = _render_and_capture_tex(tmp_path, selection, profile)
    assert r"Built it with \textbf{Python} and speed." in tex
    assert "**" not in tex


def test_bold_marker_content_is_still_latex_escaped(tmp_path):
    profile = _profile()
    selection = _selection(
        profile,
        experiences=[{"ref_id": "exp_1", "bullets": [r"Used **C & C++** heavily."]}],
    )
    tex = _render_and_capture_tex(tmp_path, selection, profile)
    assert r"\textbf{C \& C++}" in tex


# Skills are grouped by their profile-level category, preserving skill_order.
def test_skills_are_grouped_by_category(tmp_path):
    profile = _profile(
        skills=[
            Skill(id="python", label="Python", category="Programming Languages"),
            Skill(id="ros", label="ROS", category="Tools & Frameworks"),
        ],
        experiences=[
            ProfileItem(
                id="exp_1", title="Engineer", bullets=["x"],
                demonstrated_skills=["python", "ros"],
            )
        ],
    )
    selection = _selection(profile, skill_order=["python", "ros"])
    tex = _render_and_capture_tex(tmp_path, selection, profile)
    assert r"\textbf{Programming Languages:} Python" in tex
    assert r"\textbf{Tools \& Frameworks:} ROS" in tex
