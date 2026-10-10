import frameport.civ.DirectoryMount;
import java.io.File;
import java.nio.file.Files;
import java.nio.charset.StandardCharsets;
public final class DirectoryMountTest {
    private static void check(boolean ok) { if (!ok) throw new AssertionError(); }
    public static void main(String[] args) throws Exception {
        File root = Files.createTempDirectory("frameport-obb-test").toFile();
        File image = new File(root, "modules.obb");
        Files.write(image.toPath(), new byte[] {1, 2, 3});
        DirectoryMount resolver = new DirectoryMount(root);
        check(resolver.resolve(image.getPath(), null) == null);
        File output = new File(image.getPath() + DirectoryMount.SUFFIX);
        check(output.mkdir());
        check(resolver.resolve(image.getPath(), null) == null);
        File marker = new File(output, DirectoryMount.MARKER);
        Files.write(marker.toPath(), (image.length() + ":" + image.lastModified() + "\n").getBytes(StandardCharsets.US_ASCII));
        check(output.getCanonicalPath().equals(resolver.resolve(image.getPath(), null)));
        check(image.setLastModified(1791524466000L));
        Files.write(marker.toPath(), "3:1791524466525\n".getBytes(StandardCharsets.US_ASCII));
        check(output.getCanonicalPath().equals(resolver.resolve(image.getPath(), null)));
        for (String stale : new String[] {"3:1791524467525", "4:1791524466525", "3:0",
                                         "-3:1791524466525", "3:bad", "3:99999999999999999999999"}) {
            Files.write(marker.toPath(), (stale + "\n").getBytes(StandardCharsets.US_ASCII));
            check(resolver.resolve(image.getPath(), null) == null);
        }
        Files.write(marker.toPath(), "3:1791524466525\n".getBytes(StandardCharsets.US_ASCII));
        check(resolver.resolve(image.getPath(), "encrypted") == null);
        check(resolver.resolve(new File(root, "../modules.obb").getPath(), null) == null);
        Files.write(marker.toPath(), "3:0\n".getBytes(StandardCharsets.US_ASCII));
        check(resolver.resolve(image.getPath(), null) == null);
        Files.write(marker.toPath(), (image.length() + ":" + image.lastModified() + "\nextra\n").getBytes(StandardCharsets.US_ASCII));
        check(resolver.resolve(image.getPath(), null) == null);
        Files.delete(marker.toPath());
        Files.delete(output.toPath());
        Files.createSymbolicLink(output.toPath(), root.getParentFile().toPath());
        check(resolver.resolve(image.getPath(), null) == null);
        Files.delete(output.toPath()); Files.delete(image.toPath()); Files.delete(root.toPath());
        System.out.println("verified directory resolution, stale/missing markers, encrypted and escaping paths rejected");
    }
}
