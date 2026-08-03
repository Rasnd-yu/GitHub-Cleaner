import json
import os


def filter_users_by_followers(input_file, output_file, min_followers=1000):
    """
    Filter the users, keeping only those with more followers than the given threshold

    Args:
        input_file: Path to the input JSON file
        output_file: Path to the output JSON file
        min_followers: Minimum follower count threshold

    Returns:
        int: Number of users left after filtering
    """

    # Check whether the input file exists
    if not os.path.exists(input_file):
        print(f"Error: the input file {input_file} does not exist!")
        return 0

    try:
        # Read the original JSON file
        with open(input_file, 'r', encoding='utf-8') as f:
            users_data = json.load(f)

        print(f"The original data contains {len(users_data)} users")

        # Filter the users
        filtered_users = {}
        for username, user_info in users_data.items():
            # Read the follower count, defaulting to 0 when the field is missing
            followers = user_info.get('followers', 0)

            if followers > min_followers:
                filtered_users[username] = user_info

        # Count the users left after filtering
        filtered_count = len(filtered_users)
        print(f"After filtering (followers > {min_followers}): {filtered_count} users")

        # Write the filtered data to the new file
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(filtered_users, f, ensure_ascii=False, indent=2)

        print(f"Filtered data saved to: {output_file}")

        return filtered_count

    except json.JSONDecodeError as e:
        print(f"Error: malformed JSON file - {e}")
        return 0
    except Exception as e:
        print(f"Error: exception while processing the file - {e}")
        return 0


def main():
    # Set up the file paths
    input_file = 'data/users.json'
    output_file = 'data/famous_users_filtered.json'
    min_followers = 1000

    print("=" * 50)
    print("User filtering script")
    print("=" * 50)

    # Run the filter
    filtered_count = filter_users_by_followers(input_file, output_file, min_followers)

    # Report the statistics
    if filtered_count > 0:
        print("\n" + "=" * 50)
        print("Statistics:")
        print(f"- Filter condition: followers > {min_followers}")
        print(f"- Users after filtering: {filtered_count}")
        print(f"- Output file: {output_file}")
        print("=" * 50)
    else:
        print("\nFiltering failed, please check the input file or the filter condition.")


if __name__ == "__main__":
    main()