/*
 * Licensed to the Apache Software Foundation (ASF) under one or more
 * contributor license agreements.  See the NOTICE file distributed with
 * this work for additional information regarding copyright ownership.
 * The ASF licenses this file to You under the Apache License, Version 2.0
 * (the "License"); you may not use this file except in compliance with
 * the License.  You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

import java.lang.management.ManagementFactory;

/**
 * 用于验证 Broker 容器是否存在 cgroup v2 指标探测缺陷的最小探针。
 *
 * <p>背景：RocketMQ 镜像内的 JDK 8u372(Temurin) 在 Docker Desktop(cgroup v2) 下，
 * {@link ManagementFactory#getOperatingSystemMXBean()} 会抛 NullPointerException
 * （栈顶为 jdk.internal.platform.cgroupv2.CgroupV2Subsystem.getInstance），
 * 进而使 org.apache.rocketmq.store.StoreUtil 的静态初始化失败，
 * 而 StoreUtil 被 JVM 永久标记为不可用后，Broker 上所有消息读取都会失败。
 *
 * <p>用法（在 rmqbroker 容器内执行）：
 * <pre>
 *   docker cp OsMxBeanProbe.java rmqbroker:/tmp/
 *   docker exec rmqbroker sh -c 'cd /tmp && /opt/java/openjdk/bin/javac OsMxBeanProbe.java'
 *   # 复现问题
 *   docker exec rmqbroker sh -c 'cd /tmp && /opt/java/openjdk/bin/java OsMxBeanProbe'
 *   # 验证修复开关
 *   docker exec rmqbroker sh -c 'cd /tmp && /opt/java/openjdk/bin/java -XX:-UseContainerSupport OsMxBeanProbe'
 * </pre>
 *
 * <p>预期结果：
 * <pre>
 *   默认参数                    -> RESULT=FAIL java.lang.NullPointerException
 *   -XX:-UseContainerSupport    -> RESULT=OK totalPhysicalMemory=...
 * </pre>
 */
public class OsMxBeanProbe {

    public static void main(String[] args) {
        System.out.println("java.version = " + System.getProperty("java.version"));
        try {
            Object bean = ManagementFactory.getOperatingSystemMXBean();
            System.out.println("RESULT=OK class=" + bean.getClass().getName());
            com.sun.management.OperatingSystemMXBean os =
                    (com.sun.management.OperatingSystemMXBean) bean;
            System.out.println("RESULT=OK totalPhysicalMemory=" + os.getTotalPhysicalMemorySize());
        } catch (Throwable t) {
            System.out.println("RESULT=FAIL " + t);
            t.printStackTrace(System.out);
        }
    }
}
