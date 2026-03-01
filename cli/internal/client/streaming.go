package client

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
)

// ListPage represents a single page from the NDJSON /list stream.
type ListPage struct {
	Folders      []FolderEntry `json:"folders"`
	Files        []FileEntry   `json:"files"`
	Done         bool          `json:"done"`
	TotalFolders int           `json:"total_folders"`
	TotalFiles   int           `json:"total_files"`
	Indexed      bool          `json:"indexed"`
}

// FolderEntry represents a folder in a listing.
type FolderEntry struct {
	Prefix string `json:"prefix"`
	Name   string `json:"name"`
}

// FileEntry represents a file in a listing.
type FileEntry struct {
	Key          string `json:"key"`
	Name         string `json:"name"`
	Size         int64  `json:"size"`
	LastModified string `json:"last_modified"`
}

// StreamList reads NDJSON pages from the /list endpoint.
// It calls the callback for each page. The caller should stop
// when page.Done is true.
func StreamList(resp *http.Response, callback func(page ListPage) error) error {
	defer resp.Body.Close()
	scanner := bufio.NewScanner(resp.Body)
	// Allow up to 10MB lines for large directory listings
	scanner.Buffer(make([]byte, 0, 64*1024), 10*1024*1024)

	for scanner.Scan() {
		line := scanner.Bytes()
		if len(line) == 0 {
			continue
		}
		var page ListPage
		if err := json.Unmarshal(line, &page); err != nil {
			return fmt.Errorf("failed to parse NDJSON line: %w", err)
		}
		if err := callback(page); err != nil {
			return err
		}
		if page.Done {
			break
		}
	}
	return scanner.Err()
}

// StreamListCollect reads all pages and returns combined folders and files.
func StreamListCollect(resp *http.Response) ([]FolderEntry, []FileEntry, error) {
	var folders []FolderEntry
	var files []FileEntry
	err := StreamList(resp, func(page ListPage) error {
		folders = append(folders, page.Folders...)
		files = append(files, page.Files...)
		return nil
	})
	return folders, files, err
}

// ReadJSON is a generic helper to read a JSON response body.
func ReadJSON(body io.ReadCloser, v interface{}) error {
	defer body.Close()
	return json.NewDecoder(body).Decode(v)
}
