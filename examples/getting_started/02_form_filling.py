"""
Getting Started Example 2: Form Filling

This example demonstrates how to:
- Navigate to a website with forms
- Fill out input fields
- Submit forms
- Handle basic form interactions

This builds on the basic search example by showing more complex interactions.

Setup: sign in to an official subscription CLI or configure an installed local model.
"""

import asyncio

from browser_use import Agent


async def main():
	# Define a form filling task
	task = """
    Go to https://httpbin.org/forms/post and fill out the contact form with:
    - Customer name: John Doe
    - Telephone: 555-123-4567
    - Email: john.doe@example.com
    - Size: Medium
    - Topping: cheese
    - Delivery time: now
    - Comments: This is a test form submission
    
    Then submit the form and tell me what response you get.
    """

	# Create and run the agent
	agent = Agent(task=task)
	await agent.run()


if __name__ == '__main__':
	asyncio.run(main())
