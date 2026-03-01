package cmd

import (
	"fmt"
	"time"

	"github.com/spf13/cobra"

	"github.com/ashwathstephen/sairo/cli/internal/client"
	"github.com/ashwathstephen/sairo/cli/internal/config"
	"github.com/ashwathstephen/sairo/cli/internal/output"
)

var statusCmd = &cobra.Command{
	Use:   "status",
	Short: "Show current session info, server health, and endpoints",
	Long: `Display connection status, user info, and server health.

Examples:
  sairo status
  sairo status --json`,
	RunE: runStatus,
}

func init() {
	rootCmd.AddCommand(statusCmd)
}

type healthDetailResp struct {
	Status          string       `json:"status"`
	Version         string       `json:"version"`
	UptimeSeconds   float64      `json:"uptime_seconds"`
	S3Endpoint      string       `json:"s3_endpoint"`
	S3Region        string       `json:"s3_region"`
	S3Connected     bool         `json:"s3_connected"`
	S3LatencyMs     float64      `json:"s3_latency_ms"`
	S3Error         string       `json:"s3_error"`
	UserCount       int          `json:"user_count"`
	BucketCount     int          `json:"bucket_count"`
	SessionHours    int          `json:"session_hours"`
	RecrawlInterval int          `json:"recrawl_interval"`
	DBWritable      bool         `json:"db_writable"`
	Buckets         []bucketInfo `json:"buckets"`
}

type bucketInfo struct {
	Name         string `json:"name"`
	Indexed      bool   `json:"indexed"`
	Status       string `json:"status"`
	TotalObjects int64  `json:"total_objects"`
	TotalSize    int64  `json:"total_size"`
	LastCrawl    string `json:"last_crawl"`
	Crawling     bool   `json:"crawling"`
}

func runStatus(cmd *cobra.Command, args []string) error {
	c, err := newClient()
	if err != nil {
		return err
	}

	profile := resolveProfile()
	prof, _ := config.LoadProfile(profile)

	// Fetch user info and health in parallel
	var me *client.MeResponse
	var health healthDetailResp
	var meErr, healthErr error

	done := make(chan struct{})
	go func() {
		me, meErr = c.Me()
		done <- struct{}{}
	}()
	go func() {
		healthErr = c.Get("/api/health-detail", nil, &health)
		done <- struct{}{}
	}()
	<-done
	<-done

	if meErr != nil {
		return fmt.Errorf("failed to get user info: %w", meErr)
	}

	if flagJSON {
		var totalObjects int64
		var totalSize int64
		for _, b := range health.Buckets {
			totalObjects += b.TotalObjects
			totalSize += b.TotalSize
		}
		return output.JSON(map[string]interface{}{
			"profile":        profile,
			"server":         prof.URL,
			"username":       me.Username,
			"role":           me.Role,
			"session_expires": me.ExpiresAtString(),
			"version":        health.Version,
			"s3_endpoint":    health.S3Endpoint,
			"s3_connected":   health.S3Connected,
			"s3_latency_ms":  health.S3LatencyMs,
			"bucket_count":   health.BucketCount,
			"total_objects":  totalObjects,
			"total_size":     totalSize,
		})
	}

	fmt.Println()
	output.Info("Profile", profile)
	output.Info("Server", prof.URL)
	output.Info("User", fmt.Sprintf("%s (%s)", me.Username, me.Role))

	// Session expiry
	if expiresAt := me.ExpiresAtString(); expiresAt != "" {
		if t, err := output.ParseTime(expiresAt); err == nil {
			remaining := time.Until(t)
			if remaining > 0 {
				output.Info("Session", fmt.Sprintf("expires in %s", output.HumanDuration(remaining)))
			} else {
				output.Info("Session", output.Red("expired"))
			}
		}
	}

	if healthErr == nil {
		output.Info("Version", health.Version)
		s3Status := health.S3Endpoint
		if health.S3Connected {
			s3Status += fmt.Sprintf(" (%s, %dms)", output.Green("connected"), int(health.S3LatencyMs))
		} else {
			s3Status += fmt.Sprintf(" (%s)", output.Red("disconnected"))
		}
		output.Info("S3", s3Status)

		// Summary table
		var totalObjects int64
		var totalSize int64
		var lastCrawlTime time.Time
		for _, b := range health.Buckets {
			totalObjects += b.TotalObjects
			totalSize += b.TotalSize
			if t, err := output.ParseTime(b.LastCrawl); err == nil && t.After(lastCrawlTime) {
				lastCrawlTime = t
			}
		}

		fmt.Println()
		tbl := output.NewTable("BUCKETS", "OBJECTS", "TOTAL SIZE", "LAST CRAWL")
		lastCrawl := "-"
		if !lastCrawlTime.IsZero() {
			lastCrawl = output.HumanTime(lastCrawlTime)
		}
		tbl.AddRow(
			fmt.Sprintf("%d", health.BucketCount),
			output.FormatNumber(totalObjects),
			output.HumanSize(totalSize),
			lastCrawl,
		)
		tbl.Render()
	}

	fmt.Println()
	return nil
}
