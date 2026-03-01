package cmd

import (
	"bufio"
	"fmt"
	"os"
	"strings"

	"github.com/spf13/cobra"

	"github.com/ashwathstephen/sairo/cli/internal/output"
)

var rbForce bool

var mbCmd = &cobra.Command{
	Use:   "mb <bucket>",
	Short: "Create a new bucket",
	Long: `Create a new S3 bucket.

Examples:
  sairo mb test-bucket
  sairo mb staging-data`,
	Args: cobra.ExactArgs(1),
	RunE: runMb,
}

var rbCmd = &cobra.Command{
	Use:   "rb <bucket>",
	Short: "Remove a bucket",
	Long: `Delete an S3 bucket and its local index.

Examples:
  sairo rb test-bucket
  sairo rb old-bucket --force`,
	Args: cobra.ExactArgs(1),
	RunE: runRb,
}

func init() {
	rbCmd.Flags().BoolVarP(&rbForce, "force", "f", false, "Skip confirmation")
	rootCmd.AddCommand(mbCmd)
	rootCmd.AddCommand(rbCmd)
}

func runMb(cmd *cobra.Command, args []string) error {
	c, err := newClient()
	if err != nil {
		return err
	}

	name := args[0]
	payload := map[string]string{"name": name}
	var resp map[string]interface{}
	if err := c.Post("/api/buckets", payload, &resp); err != nil {
		return err
	}

	if flagJSON {
		return output.JSON(resp)
	}

	output.Success("Created bucket: %s", name)
	return nil
}

func runRb(cmd *cobra.Command, args []string) error {
	c, err := newClient()
	if err != nil {
		return err
	}

	bucket := args[0]

	if !rbForce {
		fmt.Printf("Delete bucket %s? [y/N]: ", bucket)
		reader := bufio.NewReader(os.Stdin)
		answer, _ := reader.ReadString('\n')
		answer = strings.TrimSpace(strings.ToLower(answer))
		if answer != "y" && answer != "yes" {
			fmt.Println("Cancelled.")
			os.Exit(5)
		}
	}

	var resp map[string]interface{}
	if err := c.Delete(fmt.Sprintf("/api/buckets/%s", bucket), &resp); err != nil {
		return err
	}

	if flagJSON {
		return output.JSON(resp)
	}

	output.Success("Deleted bucket: %s", bucket)
	return nil
}
