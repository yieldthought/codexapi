from codexapi import Agent, agent


def main():
    print(agent("Say hello in one sentence."))

    session = Agent()
    print(session("Summarize the current directory."))
    print(session("Now list three follow-up questions."))


if __name__ == "__main__":
    main()
