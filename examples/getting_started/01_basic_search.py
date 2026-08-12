"""Run a basic search with an authenticated subscription CLI or configured local model."""

import asyncio

from browser_use import Agent


async def main():
	task = "Search Google for 'what is browser automation' and tell me the top 3 results"
	agent = Agent(task=task)
	await agent.run()


if __name__ == '__main__':
	asyncio.run(main())
