package cmd

import (
	"fmt"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/spf13/cobra"

	"github.com/ashwathstephen/sairo/cli/internal/client"
	"github.com/ashwathstephen/sairo/cli/internal/output"
)

var (
	watchInterval int
	watchFilter   string
)

var watchCmd = &cobra.Command{
	Use:   "watch <bucket>[/prefix]",
	Short: "Watch for changes in real-time",
	Long: `Poll for file changes and display a live diff stream.

Examples:
  sairo watch my-bucket/logs/2026-03-01/
  sairo watch my-bucket/ --interval 10`,
	Args: cobra.ExactArgs(1),
	RunE: runWatch,
}

func init() {
	watchCmd.Flags().IntVarP(&watchInterval, "interval", "i", 30, "Poll interval in seconds")
	watchCmd.Flags().StringVarP(&watchFilter, "filter", "f", "", "Glob pattern to filter")
	rootCmd.AddCommand(watchCmd)
}

func runWatch(cmd *cobra.Command, args []string) error {
	c, err := newClient()
	if err != nil {
		return err
	}

	bucket, prefix := parseBucketPath(args[0])

	fmt.Printf("Watching %s/%s (every %ds, Ctrl+C to stop)\n\n", bucket, prefix, watchInterval)

	// Catch interrupt
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

	// Initial snapshot
	snapshot, err := fetchSnapshot(c, bucket, prefix)
	if err != nil {
		return err
	}

	ticker := time.NewTicker(time.Duration(watchInterval) * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-sigCh:
			fmt.Println("\nStopped.")
			return nil
		case <-ticker.C:
			newSnapshot, err := fetchSnapshot(c, bucket, prefix)
			if err != nil {
				output.Error("poll failed: %s", err)
				continue
			}
			printDiffs(snapshot, newSnapshot)
			snapshot = newSnapshot
		}
	}
}

type fileSnapshot struct {
	Key          string
	Size         int64
	LastModified string
}

func fetchSnapshot(c *client.Client, bucket, prefix string) (map[string]fileSnapshot, error) {
	params := url.Values{}
	if prefix != "" {
		params.Set("prefix", prefix)
	}

	resp, err := c.GetStream(fmt.Sprintf("/api/buckets/%s/list", bucket), params)
	if err != nil {
		return nil, err
	}

	_, files, err := client.StreamListCollect(resp)
	if err != nil {
		return nil, err
	}

	snapshot := make(map[string]fileSnapshot, len(files))
	for _, f := range files {
		if watchFilter != "" {
			if matched, _ := filepath.Match(watchFilter, f.Name); !matched {
				continue
			}
		}
		key := f.Key
		if key == "" {
			key = f.Name
		}
		snapshot[key] = fileSnapshot{
			Key:          key,
			Size:         f.Size,
			LastModified: f.LastModified,
		}
	}
	return snapshot, nil
}

func printDiffs(old, new map[string]fileSnapshot) {
	now := time.Now().Format("15:04:05")

	// Added
	for key, f := range new {
		if _, exists := old[key]; !exists {
			fmt.Printf("  %s  %s %s  %s\n", output.Dim(now), output.Green("+"), f.Key, output.HumanSize(f.Size))
		}
	}

	// Modified
	for key, newF := range new {
		if oldF, exists := old[key]; exists {
			if oldF.Size != newF.Size || oldF.LastModified != newF.LastModified {
				fmt.Printf("  %s  %s %s  %s → %s\n", output.Dim(now), output.Yellow("~"), newF.Key,
					output.HumanSize(oldF.Size), output.HumanSize(newF.Size))
			}
		}
	}

	// Removed
	for key, f := range old {
		if _, exists := new[key]; !exists {
			fmt.Printf("  %s  %s %s  %s\n", output.Dim(now), output.Red("-"), f.Key, output.HumanSize(f.Size))
		}
	}
}
