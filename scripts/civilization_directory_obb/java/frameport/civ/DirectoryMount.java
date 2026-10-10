package frameport.civ;

import java.io.File;
import java.io.IOException;
import java.io.BufferedReader;
import java.io.FileReader;

/** Resolve only this game's validated, unchanged, pre-extracted expansion files. */
public final class DirectoryMount {
    public static final String SUFFIX = ".frameport-extracted";
    public static final String MARKER = ".frameport-obb-ready";
    private final File root;
    public DirectoryMount(File root) { this.root = root; }

    public String describe(String rawPath, String key) {
        try {
            File image = rawPath == null ? null : new File(rawPath).getCanonicalFile();
            File directory = image == null ? null : new File(image.getPath() + SUFFIX).getCanonicalFile();
            File marker = directory == null ? null : new File(directory, MARKER).getCanonicalFile();
            String ready = null;
            if (marker != null && marker.isFile() && marker.length() <= 80) {
                try (BufferedReader reader = new BufferedReader(new FileReader(marker))) {
                    ready = reader.readLine();
                }
            }
            return "root=" + root.getCanonicalPath() + " source=" + image
                + " sourceFile=" + (image != null && image.isFile())
                + " directory=" + directory + " readable=" + (directory != null && directory.canRead())
                + " ready=" + ready + " expected=" + (image == null ? null : image.length() + ":" + image.lastModified())
                + " keyPresent=" + (key != null);
        } catch (IOException | SecurityException error) {
            return "resolver error=" + error.getClass().getSimpleName() + ":" + error.getMessage();
        }
    }

    private static boolean matchesStamp(File image, String stamp) {
        if (stamp == null || !stamp.matches("[0-9]+:[0-9]+")) return false;
        String[] fields = stamp.split(":", -1);
        try {
            long length = Long.parseLong(fields[0]);
            long modified = Long.parseLong(fields[1]);
            // Lepton's Android File.lastModified truncates the host timestamp to
            // whole seconds. Compare at the precision exposed by both systems.
            return image.length() == length && modified / 1000 == image.lastModified() / 1000;
        } catch (NumberFormatException error) {
            return false;
        }
    }

    public String resolve(String rawPath, String key) {
        if (rawPath == null || key != null) return null;
        try {
            File image = new File(rawPath).getCanonicalFile();
            File parent = image.getParentFile();
            if (parent == null || !parent.equals(root.getCanonicalFile()) || !image.isFile()
                    || !image.getName().endsWith(".obb")) return null;
            File directory = new File(image.getPath() + SUFFIX).getCanonicalFile();
            if (!directory.getParentFile().equals(parent) || !directory.isDirectory()) return null;
            File marker = new File(directory, MARKER).getCanonicalFile();
            if (!marker.getParentFile().equals(directory) || !marker.isFile() || marker.length() > 80) return null;
            try (BufferedReader reader = new BufferedReader(new FileReader(marker))) {
                if (!matchesStamp(image, reader.readLine()) || reader.readLine() != null) return null;
            }
            return directory.getPath();
        } catch (IOException | SecurityException error) {
            return null;
        }
    }
}
