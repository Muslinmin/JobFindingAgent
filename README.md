# JobFindingAgent

An agentic job application tracker. Interact in natural language — the system handles discovery, storage, scoring, and status tracking automatically.

## Requirements

- [Miniconda](https://docs.conda.io/en/latest/miniconda.html) or Anaconda
- An [Anthropic API key](https://console.anthropic.com/)
- A [Tavily API key](https://tavily.com/)

## Installation

### 1. Clone the repository

```bash
git clone <repo-url>
cd JobFindingAgent
```

### 2. Create the conda environment

```bash
conda env create -f environment.yml
conda activate job-finder
```

### 3. Configure environment variables

Create a `.env` file in the project root:

```env
ANTHROPIC_API_KEY=your-anthropic-key
TAVILY_API_KEY=your-tavily-key
DB_PATH=./jobs.db
LOG_LEVEL=INFO
```

## Running

### Start the API server

```bash
uvicorn app.main:app --reload
```

### Start the frontend

```bash
streamlit run app/frontend/app.py
```

## Testing

```bash
# Run all tests
pytest

# Unit tests only
pytest tests/unit/

# With coverage
pytest --cov=app --cov-report=term-missing
```
