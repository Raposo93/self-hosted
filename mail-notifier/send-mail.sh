#!/usr/bin/env bash

set -Eeuo pipefail

usage() {
    cat <<'EOF'
Usage:
  send-mail.sh --to ADDRESS --subject SUBJECT [OPTIONS]

Options:
  -a, --account NAME       msmtp account
  -f, --from ADDRESS       sender address
  -t, --to ADDRESS         recipient address
  -s, --subject SUBJECT    email subject
  -h, --help               show this help

The message body is read from standard input.
EOF
}

ACCOUNT=""
FROM=""
TO=""
SUBJECT=""

while (($# > 0)); do
    case "$1" in
        -a|--account)
            [[ $# -ge 2 ]] || {
                echo "Error: $1 requires a value." >&2
                exit 2
            }

            ACCOUNT="$2"
            shift 2
            ;;

        -f|--from)
            [[ $# -ge 2 ]] || {
                echo "Error: $1 requires a value." >&2
                exit 2
            }

            FROM="$2"
            shift 2
            ;;

        -t|--to)
            [[ $# -ge 2 ]] || {
                echo "Error: $1 requires a value." >&2
                exit 2
            }

            TO="$2"
            shift 2
            ;;

        -s|--subject)
            [[ $# -ge 2 ]] || {
                echo "Error: $1 requires a value." >&2
                exit 2
            }

            SUBJECT="$2"
            shift 2
            ;;

        -h|--help)
            usage
            exit 0
            ;;

        --)
            shift
            break
            ;;

        -*)
            echo "Error: Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;

        *)
            echo "Error: Unexpected argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

[[ -n "$TO" ]] || {
    echo "Error: Missing --to." >&2
    exit 2
}

[[ -n "$SUBJECT" ]] || {
    echo "Error: Missing --subject." >&2
    exit 2
}

if [[ -t 0 ]]; then
    echo "Error: Message body must be provided through standard input." >&2
    exit 2
fi

MESSAGE="$(cat)"

[[ -n "$MESSAGE" ]] || {
    echo "Error: Message body is empty." >&2
    exit 2
}

msmtp_args=()

if [[ -n "$ACCOUNT" ]]; then
    msmtp_args+=(--account="$ACCOUNT")
fi

{
    if [[ -n "$FROM" ]]; then
        printf 'From: %s\n' "$FROM"
    fi

    printf 'To: %s\n' "$TO"
    printf 'Subject: %s\n' "$SUBJECT"
    printf 'Content-Type: text/plain; charset=UTF-8\n'
    printf 'Content-Transfer-Encoding: 8bit\n'
    printf '\n'
    printf '%s\n' "$MESSAGE"
} | msmtp "${msmtp_args[@]}" -- "$TO"