"""
Getting Started Example 3: Data Extraction

This example demonstrates how to:
- Navigate to a website with structured data
- Extract specific information from the page
- Process and organize the extracted data
- Return structured results

This builds on previous examples by showing how to get valuable data from websites.

Setup: sign in to an official subscription CLI or configure an installed local model.
"""

import asyncio

from browser_use import Agent


async def main():
	# Define a data extraction task
	task = """
    Go to https://quotes.toscrape.com/ and extract the following information:
    - The first 5 quotes on the page
    - The author of each quote
    - The tags associated with each quote
    
    Present the information in a clear, structured format like:
    Quote 1: "[quote text]" - Author: [author name] - Tags: [tag1, tag2, ...]
    Quote 2: "[quote text]" - Author: [author name] - Tags: [tag1, tag2, ...]
    etc.
    """

	# Create and run the agent
	agent = Agent(task=task)
	await agent.run()


if __name__ == '__main__':
	asyncio.run(main())
