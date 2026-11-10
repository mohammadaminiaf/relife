import jadx.api.*;
import java.io.File;
import java.nio.file.*;
import java.util.*;

/** Export readable Java and resources with REA's verified, unmodified JADX engine. */
class ExportApk {
    public static void main(String[] args) throws Exception {
        if (args.length != 2) throw new IllegalArgumentException("Usage: ExportApk APK OUTPUT");
        JadxArgs options = new JadxArgs();
        options.setInputFile(new File(args[0]));
        options.setOutDir(new File(args[1]));
        options.setThreadsCount(4);
        try (JadxDecompiler jadx = new JadxDecompiler(options)) {
            jadx.load();
            for (String name : List.of("com.ghbook.reader.gui.logic.r", "a0.d", "p0.a", "u.f", "r0.a", "r0.b")) {
                JavaClass cls = jadx.searchJavaClassByOrigFullName(name);
                if (cls == null) continue;
                Path path = Path.of(args[1], "priority", name.replace('.', '/') + ".java");
                Files.createDirectories(path.getParent());
                Files.writeString(path, cls.getCode());
                System.out.println("Exported " + name);
            }
            jadx.save();
            System.out.println("Export finished; JADX errors=" + jadx.getErrorsCount()
                + ", warnings=" + jadx.getWarnsCount());
        }
    }
}
