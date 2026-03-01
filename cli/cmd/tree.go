package cmd

import (
	"fmt"
	"net/url"

	"github.com/spf13/cobra"

	"github.com/ashwathstephen/sairo/cli/internal/client"
	"github.com/ashwathstephen/sairo/cli/internal/output"
)

var (
	treeDepth    int
	treeDirsOnly bool
	treeSize     bool
)

var treeCmd = &cobra.Command{
	Use:   "tree <bucket[/prefix]>",
	Short: "Display directory tree structure",
	Long: `Show a tree view of the bucket directory structure.

Examples:
  sairo tree my-bucket/
  sairo tree my-bucket/logs/ -d 2 --size
  sairo tree my-bucket/ --dirs-only`,
	Args: cobra.ExactArgs(1),
	RunE: runTree,
}

func init() {
	treeCmd.Flags().IntVarP(&treeDepth, "depth", "d", 3, "Max depth to display")
	treeCmd.Flags().BoolVar(&treeDirsOnly, "dirs-only", false, "Only show directories")
	treeCmd.Flags().BoolVar(&treeSize, "size", false, "Show folder sizes")
	rootCmd.AddCommand(treeCmd)
}

type treeNode struct {
	Name     string
	IsDir    bool
	Size     int64
	Children []*treeNode
}

func runTree(cmd *cobra.Command, args []string) error {
	c, err := newClient()
	if err != nil {
		return err
	}

	bucket, prefix := parseBucketPath(args[0])

	root := &treeNode{Name: prefix, IsDir: true}
	if err := buildTree(c, bucket, prefix, root, 0); err != nil {
		return err
	}

	if flagJSON {
		return output.JSON(root)
	}

	// Print root name
	rootLabel := prefix
	if rootLabel == "" {
		rootLabel = bucket + "/"
	}
	if treeSize && root.Size > 0 {
		rootLabel += fmt.Sprintf(" (%s)", output.HumanSize(root.Size))
	}
	fmt.Println(rootLabel)

	printTree(root.Children, "")
	return nil
}

func buildTree(c *client.Client, bucket, prefix string, node *treeNode, depth int) error {
	if depth >= treeDepth {
		return nil
	}

	params := url.Values{}
	if prefix != "" {
		params.Set("prefix", prefix)
	}

	resp, err := c.GetStream(fmt.Sprintf("/api/buckets/%s/list", bucket), params)
	if err != nil {
		return err
	}

	folders, files, err := client.StreamListCollect(resp)
	if err != nil {
		return err
	}

	// Get folder size if requested
	if treeSize {
		var sizeResp struct {
			TotalSize int64 `json:"total_size"`
		}
		sizeParams := url.Values{}
		if prefix != "" {
			sizeParams.Set("prefix", prefix)
		}
		if err := c.Get(fmt.Sprintf("/api/buckets/%s/folder-size", bucket), sizeParams, &sizeResp); err == nil {
			node.Size = sizeResp.TotalSize
		}
	}

	for _, f := range folders {
		child := &treeNode{Name: f.Name, IsDir: true}
		node.Children = append(node.Children, child)
		if err := buildTree(c, bucket, f.Prefix, child, depth+1); err != nil {
			return err
		}
	}

	if !treeDirsOnly {
		for _, f := range files {
			node.Children = append(node.Children, &treeNode{
				Name:  f.Name,
				IsDir: false,
				Size:  f.Size,
			})
		}
	}

	return nil
}

func printTree(nodes []*treeNode, indent string) {
	for i, node := range nodes {
		isLast := i == len(nodes)-1
		connector := "├── "
		childIndent := "│   "
		if isLast {
			connector = "└── "
			childIndent = "    "
		}

		label := node.Name
		if node.IsDir {
			label = output.Cyan(label)
			if treeSize && node.Size > 0 {
				label += fmt.Sprintf(" (%s)", output.HumanSize(node.Size))
			}
		} else if treeSize {
			label += fmt.Sprintf(" (%s)", output.HumanSize(node.Size))
		}

		fmt.Printf("%s%s%s\n", indent, connector, label)

		if node.IsDir && len(node.Children) > 0 {
			printTree(node.Children, indent+childIndent)
		}
	}
}
