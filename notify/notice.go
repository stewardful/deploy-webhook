// Package notify receives release hints. It never downloads or executes an update.
package notify

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"
)

var commitPattern = regexp.MustCompile(`^[a-f0-9]{40}$`)
var repositoryPattern = regexp.MustCompile(`^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`)
var devTag = regexp.MustCompile(`^dev-([1-9][0-9]{0,19})$`)
var stableTag = regexp.MustCompile(`^v(0|[1-9][0-9]{0,9})\.(0|[1-9][0-9]{0,9})\.(0|[1-9][0-9]{0,9})$`)

// Notice is a hint to recheck a trusted Release, never an installation instruction.
type Notice struct {
	Repository  string    `json:"repository"`
	Channel     string    `json:"channel"`
	Tag         string    `json:"tag"`
	ReleaseID   int64     `json:"release_id"`
	PublishedAt time.Time `json:"published_at"`
	Commit      string    `json:"commit"`
}

func validRepository(value string) bool {
	return len(value) <= 200 && repositoryPattern.MatchString(value) && !strings.Contains(value, "..")
}

func (n Notice) Valid() bool {
	if !validRepository(n.Repository) || n.ReleaseID <= 0 || n.PublishedAt.IsZero() || !commitPattern.MatchString(n.Commit) {
		return false
	}
	switch n.Channel {
	case "dev":
		match := devTag.FindStringSubmatch(n.Tag)
		if match == nil {
			return false
		}
		_, err := strconv.ParseUint(match[1], 10, 64)
		return err == nil
	case "stable":
		return stableTag.MatchString(n.Tag)
	}
	return false
}

type releasePayload struct {
	Action     string `json:"action"`
	Repository struct {
		FullName string `json:"full_name"`
		ID       int64  `json:"id"`
	} `json:"repository"`
	Release struct {
		ID          int64     `json:"id"`
		Tag         string    `json:"tag_name"`
		Commit      string    `json:"target_commitish"`
		Draft       bool      `json:"draft"`
		Prerelease  bool      `json:"prerelease"`
		PublishedAt time.Time `json:"published_at"`
	} `json:"release"`
}

func (p releasePayload) notice(repository string) (Notice, bool, error) {
	if p.Repository.FullName != repository || p.Repository.ID <= 0 {
		return Notice{}, false, fmt.Errorf("repository differs")
	}
	if p.Action != "published" || p.Release.Draft || p.Release.Tag == "dev-latest" {
		return Notice{}, false, nil
	}
	channel := "stable"
	if p.Release.Prerelease {
		channel = "dev"
	}
	n := Notice{Repository: repository, Channel: channel, Tag: p.Release.Tag,
		ReleaseID: p.Release.ID, PublishedAt: p.Release.PublishedAt.UTC(), Commit: p.Release.Commit}
	if !n.Valid() {
		return Notice{}, false, fmt.Errorf("unsupported release")
	}
	return n, true, nil
}
