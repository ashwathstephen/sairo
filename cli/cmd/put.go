package cmd

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"os"
	"path/filepath"
	"strings"

	"github.com/spf13/cobra"

	"github.com/ashwathstephen/sairo/cli/internal/client"
	"github.com/ashwathstephen/sairo/cli/internal/output"
)

var (
	putRecursive bool
	putParallel  int
	putDryRun    bool
	putExclude   string
)

var putCmd = &cobra.Command{
	Use:   "put <local-path> <bucket>[/prefix]",
	Short: "Upload files to S3 via Sairo",
	Long: `Upload a file or directory to a bucket.

Examples:
  sairo put ./report.pdf my-bucket/reports/
  sairo put ./data/ my-bucket/backups/2026-03/ -r
  sairo put ./logs/ my-bucket/logs/ -r --exclude "*.tmp"`,
	Args: cobra.ExactArgs(2),
	RunE: runPut,
}

func init() {
	putCmd.Flags().BoolVarP(&putRecursive, "recursive", "r", false, "Upload directory contents")
	putCmd.Flags().IntVar(&putParallel, "parallel", 4, "Concurrent uploads")
	putCmd.Flags().BoolVar(&putDryRun, "dry-run", false, "Show what would be uploaded")
	putCmd.Flags().StringVar(&putExclude, "exclude", "", "Glob pattern to exclude")
	rootCmd.AddCommand(putCmd)
}

func runPut(cmd *cobra.Command, args []string) error {
	c, err := newClient()
	if err != nil {
		return err
	}

	localPath := args[0]
	bucket, prefix := parseBucketPath(args[1])

	info, err := os.Stat(localPath)
	if err != nil {
		return fmt.Errorf("cannot access %s: %w", localPath, err)
	}

	if info.IsDir() {
		if !putRecursive {
			return fmt.Errorf("%s is a directory (use -r for recursive upload)", localPath)
		}
		return uploadDirectory(c, bucket, prefix, localPath)
	}

	return uploadSingleFile(c, bucket, prefix, localPath)
}

func uploadSingleFile(c *client.Client, bucket, prefix, localPath string) error {
	filename := filepath.Base(localPath)
	destKey := prefix + filename

	if putDryRun {
		info, _ := os.Stat(localPath)
		fmt.Printf("Would upload: %s → %s/%s (%s)\n", localPath, bucket, destKey, output.HumanSize(info.Size()))
		return nil
	}

	result, err := uploadFile(c, bucket, prefix, localPath)
	if err != nil {
		return err
	}

	if flagJSON {
		return output.JSON(result)
	}

	if uploaded, ok := result["uploaded"].([]interface{}); ok && len(uploaded) > 0 {
		output.Success("Uploaded %s/%s", bucket, destKey)
	}
	return nil
}

func uploadDirectory(c *client.Client, bucket, prefix, dirPath string) error {
	var files []string
	err := filepath.Walk(dirPath, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if info.IsDir() {
			return nil
		}
		// Check exclude pattern
		if putExclude != "" {
			if matched, _ := filepath.Match(putExclude, filepath.Base(path)); matched {
				return nil
			}
		}
		files = append(files, path)
		return nil
	})
	if err != nil {
		return err
	}

	if len(files) == 0 {
		fmt.Println("No files to upload.")
		return nil
	}

	var totalSize int64
	for _, f := range files {
		if info, err := os.Stat(f); err == nil {
			totalSize += info.Size()
		}
	}

	if putDryRun {
		fmt.Printf("Would upload %d files (%s)\n", len(files), output.HumanSize(totalSize))
		for _, f := range files {
			info, _ := os.Stat(f)
			rel, _ := filepath.Rel(dirPath, f)
			fmt.Printf("  %s → %s/%s%s (%s)\n", f, bucket, prefix, rel, output.HumanSize(info.Size()))
		}
		return nil
	}

	if !flagQuiet && output.IsTTY() {
		fmt.Printf("Uploading %d files (%s)...\n", len(files), output.HumanSize(totalSize))
	}

	var uploadedCount int
	var errCount int
	for i, filePath := range files {
		rel, _ := filepath.Rel(dirPath, filePath)
		// Normalize path separators for S3
		relKey := strings.ReplaceAll(rel, string(os.PathSeparator), "/")
		filePrefix := prefix + filepath.Dir(relKey) + "/"
		if filePrefix == prefix+"./" {
			filePrefix = prefix
		}

		_, err := uploadFile(c, bucket, filePrefix, filePath)
		if err != nil {
			errCount++
			output.Error("[%d/%d] %s: %s", i+1, len(files), rel, err)
		} else {
			uploadedCount++
			if output.IsTTY() && !flagQuiet {
				info, _ := os.Stat(filePath)
				fmt.Printf("  [%d/%d] %s  %s  %s\n", i+1, len(files), rel, output.HumanSize(info.Size()), output.Green("✓"))
			}
		}
	}

	if errCount > 0 {
		return fmt.Errorf("%d files failed to upload", errCount)
	}

	if flagJSON {
		return output.JSON(map[string]interface{}{
			"uploaded":   uploadedCount,
			"total_size": totalSize,
		})
	}

	output.Success("Uploaded %d files (%s)", uploadedCount, output.HumanSize(totalSize))
	return nil
}

func uploadFile(c *client.Client, bucket, prefix, localPath string) (map[string]interface{}, error) {
	file, err := os.Open(localPath)
	if err != nil {
		return nil, err
	}
	defer file.Close()

	// Build multipart form
	var buf bytes.Buffer
	writer := multipart.NewWriter(&buf)

	_ = writer.WriteField("prefix", prefix)

	part, err := writer.CreateFormFile("files", filepath.Base(localPath))
	if err != nil {
		return nil, err
	}
	if _, err := io.Copy(part, file); err != nil {
		return nil, err
	}
	writer.Close()

	// Build request manually (need multipart content type)
	reqURL := c.BaseURL + fmt.Sprintf("/api/buckets/%s/upload", bucket)
	if c.EndpointID != "" && c.EndpointID != "default" {
		reqURL = c.BaseURL + fmt.Sprintf("/api/e/%s/buckets/%s/upload", c.EndpointID, bucket)
	}

	req, err := http.NewRequest("POST", reqURL, &buf)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", writer.FormDataContentType())

	if c.Token != "" {
		if strings.HasPrefix(c.Token, "sairo_") {
			req.Header.Set("Authorization", "Bearer "+c.Token)
		} else {
			req.AddCookie(&http.Cookie{Name: "access_token", Value: c.Token})
		}
	}

	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, fmt.Errorf("upload failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		body, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("upload failed (HTTP %d): %s", resp.StatusCode, string(body))
	}

	var result map[string]interface{}
	json.NewDecoder(resp.Body).Decode(&result)
	return result, nil
}
