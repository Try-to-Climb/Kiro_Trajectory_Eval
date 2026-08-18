#!/bin/bash
# Inject today's info to verify that the LLM can see it
EVENT=$(cat)
echo "Extra info: today's lucky number is 42, and the lucky color is purple."
echo "Extra info: the user's pet is called \"Xiaohua\", a cat."
